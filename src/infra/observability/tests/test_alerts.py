"""O04 one-time transition and fail-closed deployment boundaries, no Docker daemon."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

OPS=Path(__file__).resolve().parents[1]/'ops'
sys.path.insert(0,str(OPS))
import alerts as module
from test_restart_check import Runtime


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'alerts').mkdir()
        self.runtime=Runtime(self.root);self.runtime.root=self.root;self.runtime.docker=object()
        self.runtime.state['config_id']='fixture'
        self.state={'enabled':False};self.path=self.root/'transition.json'
        self.record={'baseline':{'containers':self.runtime.containers()},'prometheus_intent':None}
        self.calls=[]

    def compose(self,docker,root,args,**kw):
        stored=json.loads(self.path.read_text())
        self.assertTrue(stored['prometheus_intent'])
        self.calls.append(args)
        self.runtime.items['obs-prometheus']['id']='f'*64

    def test_single_replacement_is_durable_and_scoped_to_prometheus(self):
        with patch.object(module.op,'config_check'),patch.object(module.op,'compose',self.compose):
            module.attach(self.runtime,self.state,self.record,self.path)
            module.attach(self.runtime,self.state,self.record,self.path)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0][-1],'obs-prometheus')
        for flag in ('--no-deps','--no-build','never','--force-recreate'):self.assertIn(flag,self.calls[0])
        self.assertTrue(json.loads(self.path.read_text())['prometheus_command_completed'])

    def test_lost_docker_response_cannot_cause_second_replacement(self):
        def lose(*args,**kw):
            self.compose(*args,**kw);raise subprocess.TimeoutExpired('docker',240)
        with patch.object(module.op,'config_check'),patch.object(module.op,'compose',lose):
            with self.assertRaises(subprocess.TimeoutExpired):module.attach(self.runtime,self.state,self.record,self.path)
            recovered=json.loads(self.path.read_text())
            module.attach(self.runtime,self.state,recovered,self.path)
        self.assertEqual(len(self.calls),1)
        self.assertNotIn('prometheus_command_completed',recovered)

    def test_foreign_container_change_refuses_any_mutation(self):
        self.runtime.items['obs-node-exporter']['id']='e'*64
        with patch.object(module.op,'compose') as compose:
            with self.assertRaisesRegex(ValueError,'CHANGED_BEFORE_ATTACH'):
                module.attach(self.runtime,self.state,self.record,self.path)
        compose.assert_not_called();self.assertFalse(self.path.exists())

    def test_missing_mount_configuration_stops_before_control_intent(self):
        with patch.object(module.op,'config_check',side_effect=ValueError('mount invalid')),patch.object(module.op,'compose') as compose:
            with self.assertRaisesRegex(ValueError,'mount invalid'):
                module.attach(self.runtime,self.state,self.record,self.path)
        compose.assert_not_called();self.assertIsNone(self.record['prometheus_intent'])

    def test_baseline_tamper_is_rejected(self):
        core={'value':'original'}
        module.retained.write_record(self.path,{'schema':1,'kind':'O04-deploy','baseline':core,
                                               'baseline_sha256':module.retained.digest(core)})
        self.assertEqual(module.read_record(self.path)['baseline'],core)
        record=json.loads(self.path.read_text());record['baseline']['value']='changed'
        module.retained.write_record(self.path,record)
        with self.assertRaisesRegex(ValueError,'RECORD_INVALID'):module.read_record(self.path)

    def test_config_drift_blocks_existing_evidence(self):
        record={'baseline':{'binding':{'config':'old'}}}
        with patch.object(module,'binding',return_value={'config':'new'}):
            with self.assertRaisesRegex(ValueError,'CONFIG_CHANGED'):module.assert_bound(self.runtime,self.state,record)

    def test_existing_collectors_compose_keeps_alert_overlay_when_enabled(self):
        self.runtime.state['config_id']='fixture'
        module.op.write_private(self.root/'.state.json',self.runtime.state)
        (self.root/'gpu').mkdir();module.op.write_private(self.root/'gpu/.state.json',{'schema':1,'config_id':'fixture','enabled':True})
        module.op.write_private(self.root/'alerts/.state.json',{'schema':1,'config_id':'fixture','enabled':True})
        class Docker:
            def run(_,args,**kw):return args
        args=module.op.compose(Docker(),self.root,['config'])
        self.assertIn(str(self.root/'alerts/prometheus-overlay.yml'),args)
        self.assertEqual(module.op.prometheus_config(self.root),self.root/'alerts/prometheus.yml')

    def test_unowned_alert_state_does_not_modify_existing_compose(self):
        module.op.write_private(self.root/'.state.json',{'config_id':'fixture'})
        module.op.write_private(self.root/'alerts/.state.json',{'schema':1,'config_id':'other','enabled':True})
        with self.assertRaises(ValueError):module.op.prometheus_config(self.root)

    def test_native_test_failure_stops_before_production_commands(self):
        self.runtime.state['data_dir']=str(self.root/'prometheus')
        with patch.object(module,'prepare',return_value=self.state), \
             patch.object(module.op,'project_containers',return_value=[{'Config':{'Labels':{'com.docker.compose.service':'obs-prometheus'}},
                'Mounts':[{'Destination':'/etc/prometheus/prometheus.yml','Source':str(self.root/'gpu/prometheus.yml'),'RW':False}]}]), \
             patch.object(module,'native_tests',side_effect=ValueError('native check failed')), \
             patch.object(module,'manager_compose') as am,patch.object(module,'attach') as attach:
            with self.assertRaisesRegex(ValueError,'native check failed'):module.deploy(self.runtime)
        am.assert_not_called();attach.assert_not_called()

    def test_production_receivers_are_explicitly_empty(self):
        cfg=json.loads((OPS.parent/'alerts/alertmanager.yml').read_text())
        self.assertEqual(cfg['receivers'],[{'name':'ops-local-only'}])
        self.assertEqual(cfg['route']['receiver'],'ops-local-only')


if __name__=='__main__':unittest.main()
