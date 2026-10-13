"""Actual Plan/Review constructors, real source mapping, bounded diagnostics.

Manager/Docker/application responses are synthetic. Private metadata and the
existing native test age tools are used; no production preparation or control.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ops_tools import backup_plan as p
from ops_tools.tests import test_backup_production_plan as previous
from ops_tools.tests import test_maintenance_control as maintenance


@unittest.skipUnless((previous.TOOLS/'age-keygen').is_file(),'cached test age unavailable')
class ConstructorMapping(unittest.TestCase):
    def setUp(self):
        self.mapping=previous.ProductionMapping('runTest'); self.mapping.setUp()
        self.addCleanup(self.mapping.doCleanups)
        self.worker=maintenance.ReviewTests('runTest'); self.worker.setUp()
        self.addCleanup(self.worker.doCleanups)

    def make(self):
        manager=self.worker.f.manager
        with patch.object(maintenance.runtime.probe,'systemd',return_value={'status':'ok','linger':True}), \
             patch.object(maintenance.m,'read_only_type',return_value=lambda *args:manager), \
             patch.object(p,'load',return_value=maintenance.m):
            plan=p.Plan(self.worker.f.directory,self.mapping.f.lf,self.mapping.home)
        self.assertIsInstance(plan.review,maintenance.m.Review)
        # This is the real constructor's shape, which the old namespace
        # fixture incorrectly replaced with an extra .langfuse attribute.
        self.assertFalse(hasattr(plan.review,'langfuse'))
        self.assertEqual(plan.review.inventory.langfuse,self.mapping.f.lf)
        self.assertEqual(plan.langfuse,self.mapping.f.lf)
        return plan

    def wired(self):
        plan=self.make(); f=self.mapping
        plan.root=f.f.root; plan.home=f.home; plan.preparation=f.plan.preparation
        review=plan.review
        review.root=f.f.root; review.directory=f.f.ops; review.cap=f.cap
        review.manifest=f.r.manifest; review.sources=f.r.sources; review.docker=f.docker
        review.inventory=maintenance.m.Inventory(f.f.root,f.f.lf,f.f.ops,f.cap,f.docker)
        review.plan=lambda:deepcopy(f.maintenance); review.state=lambda:deepcopy(f.state)
        return plan

    def test_actual_constructors_preserve_explicit_langfuse_path_without_controls(self):
        before=self.mapping.f.lf.stat(); controls=self.worker.f.manager.control_commands
        plan=self.make()
        self.assertEqual(plan.langfuse.stat().st_ino,before.st_ino)
        self.assertEqual(self.worker.f.manager.control_commands,controls)

    def test_actual_review_shape_reaches_complete_inventory_and_generation_map(self):
        plan=self.wired(); before=previous.captest.fixtures.snapshot(self.mapping.f.root)
        generation,inventory,_=plan.snapshot()
        self.assertEqual(plan.phase,'generation_mapping')
        self.assertEqual(len(generation['cold']['services']),9)
        self.assertEqual(len(generation['cold']['stores']),8)
        self.assertEqual(inventory.lf_root,self.mapping.f.lf)
        self.assertEqual(before,previous.captest.fixtures.snapshot(self.mapping.f.root))
        self.assertEqual(self.mapping.docker.exec_calls,0)

    def test_actual_constructors_mapping_and_unmocked_capacity_projection(self):
        plan=self.wired(); target=Path(plan.preparation['paths']['destination'])
        dev=target.stat().st_dev
        mount=f'1 0 {os.major(dev)}:{os.minor(dev)} / / rw - ext4 /dev/synthetic rw\n'
        text=Path.read_text
        def read(path,*args,**kwargs):
            if path==Path('/proc/self/mountinfo'): return mount
            return text(path,*args,**kwargs)
        with patch.object(Path,'read_text',read): result=plan.plan()
        self.assertEqual(result['result'],'passed'); self.assertEqual(plan.phase,'complete')
        self.assertEqual(result['source_count'],22); self.assertEqual(result['unique_image_count'],len(result['image_ids']))
        self.assertFalse(result['backup_ready']); self.assertFalse(result['identity_custody_verified'])
        self.assertFalse(result['capacity']['actual_source_size_measured'])
        self.assertEqual(result['capacity']['filesystem']['fstype'],'ext4')
        self.assertEqual(self.mapping.docker.exec_calls,0)

    def test_custom_langfuse_path_is_never_taken_from_an_invented_review_field(self):
        plan=self.wired(); plan.review.langfuse=Path('/unselected-langfuse')
        _,inventory,_=plan.snapshot()
        self.assertEqual(inventory.lf_root,self.mapping.f.lf)


class Diagnostics(unittest.TestCase):
    def probe(self,operation_error):
        review=SimpleNamespace(reader=SimpleNamespace(calls=1),docker=SimpleNamespace(calls=2,base=SimpleNamespace(calls=3)),
                               backend=SimpleNamespace(control_commands=0))
        plan=SimpleNamespace(phase='inventory_construction',review=review,module=maintenance.m)
        def failed(): raise operation_error
        plan.plan=failed
        return p.run(Path('/unread-state'),Path('/unread-langfuse'),Path('/unread-home'),
                     factory=lambda *args:plan)

    def test_builtin_failures_report_only_fixed_phase_and_exception_labels(self):
        for error,label in ((AttributeError('PRIVATE_FIELD_PATH_TOKEN'),'attribute_error'),
                            (KeyError('PRIVATE_KEY'),'key_error'),(TypeError('PRIVATE_CONTENT'),'type_error'),
                            (json.JSONDecodeError('PRIVATE_MESSAGE','PRIVATE_DOCUMENT',0),'json_decode_error')):
            result=self.probe(error)
            self.assertEqual(result['code'],'BACKUP_PRODUCTION_PLAN_UNAVAILABLE')
            self.assertEqual(result['plan_diagnostic'],dict(phase='inventory_construction',exception_kind=label,retry_requested=False))
            self.assertNotIn('PRIVATE',json.dumps(result)); self.assertEqual(result['worker_service_control_commands'],0)

    def test_typed_failure_retains_gate_and_safe_phase(self):
        result=self.probe(previous.s.Failure('BACKUP_REQUIRED_FILE_METADATA_UNAVAILABLE'))
        self.assertEqual(result['code'],'BACKUP_REQUIRED_FILE_METADATA_UNAVAILABLE')
        self.assertEqual(result['plan_diagnostic']['exception_kind'],'typed_failure')

    def test_constructor_failure_does_not_expose_exception_or_read_preparation_again(self):
        def failed(*args): raise PermissionError('PRIVATE_PREPARATION')
        result=p.run(Path('/unread-state'),Path('/unread-langfuse'),Path('/unread-home'),factory=failed)
        self.assertEqual(result['plan_diagnostic']['phase'],'constructor')
        self.assertEqual(result['plan_diagnostic']['exception_kind'],'permission_denied')
        self.assertNotIn('PRIVATE',json.dumps(result))

    def test_unknown_phase_is_not_returned_as_arbitrary_text(self):
        value=p.plan_diagnostic(SimpleNamespace(phase='PRIVATE_PATH'),RuntimeError('PRIVATE_MESSAGE'))
        self.assertEqual(value,dict(phase='constructor',exception_kind='unknown',retry_requested=False))

    def test_detail_diagnostics_preserve_new_operation_without_modifying_pinned_module(self):
        original=set(maintenance.m.DOCKER_OPERATIONS)
        value=p.detail_failure(maintenance.m,1,b'',b'Error: No such container: PRIVATE_ID')
        self.assertEqual(value['operation'],'container_inspect'); self.assertEqual(value['reason'],'CONTAINER_NOT_FOUND')
        self.assertEqual(p.safe_docker_diagnostic(maintenance.m,value),value)
        self.assertEqual(maintenance.m.DOCKER_OPERATIONS,original); self.assertNotIn('PRIVATE',json.dumps(value))

    def test_detail_command_nonzero_and_timeout_keep_bounded_diagnostic(self):
        class Base:
            def __init__(self):
                self.calls=0; self.base=SimpleNamespace(endpoint='unix:///synthetic.sock',env={})
        value=p.docker_type(Base,maintenance.m)()
        value.runner=lambda *args:(1,b'',b'Cannot connect to the Docker daemon PRIVATE_PATH')
        with self.assertRaises(maintenance.m.Failure) as caught: value.detail('a'*64)
        self.assertEqual(caught.exception.code,'BACKUP_DOCKER_DETAIL_FAILED')
        self.assertEqual(caught.exception.docker_diagnostic['reason'],'DOCKER_CONNECTION_FAILED')
        def timeout(*args): raise RuntimeError('CLIENT_DEADLINE_EXCEEDED')
        value.runner=timeout
        with self.assertRaises(maintenance.m.Failure) as caught: value.detail('a'*64)
        self.assertEqual(caught.exception.docker_diagnostic['reason'],'COMMAND_TIMEOUT')

    def test_malformed_or_extra_diagnostic_fields_are_not_returned(self):
        value=p.detail_failure(maintenance.m,1,b'',b'UNKNOWN PRIVATE_TEXT')
        for changed in (dict(value,private='PRIVATE'),dict(value,reason='PRIVATE'),dict(value,operation='PRIVATE')):
            self.assertIsNone(p.safe_docker_diagnostic(maintenance.m,changed))


if __name__=='__main__': unittest.main()
