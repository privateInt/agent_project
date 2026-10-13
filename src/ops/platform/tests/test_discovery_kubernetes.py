"""Kubernetes proposals with real HTTP metrics and synthetic API object lifecycles."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

from src.ops.platform.contracts import ReadFailure
from src.ops.platform.ops import discover as entry
from src.ops.platform.ops.discovery_kubernetes import discover_kubernetes, ROLES
from src.ops.platform.ops.discovery_metrics import probe_query
from src.ops.platform.ops.kubernetes_client import Kubectl, PART_OF, INSTALLATION, ROLE, PORT
from src.ops.platform.resource_profiles import queries
from src.ops.platform.transport import Transport
from src.ops.platform.tests.test_discovery import Fixture as HTTPFixture
from src.ops.platform.tests import test_resource_promql as native_queries


class Objects:
    def __init__(self):
        self.services, self.slices, self.calls = [], {}, []
        self.uid = 'cluster-namespace-uid'
        self.services_reads = 0; self.on_second = None
        self.server = 'https://fixture-kube.invalid:6443'; self.context = 'fixture-current'; self.insecure = False
        self.forbidden = None
        for n, role in enumerate(ROLES):
            ns = 'apps-renamed' if n < 4 else 'telemetry-renamed'
            name = 'renamed-' + role
            meta = {'name':name,'namespace':ns,'uid':'service-uid-'+role,
                    'labels':{PART_OF:'agent-project',INSTALLATION:'install-renamed',ROLE:role},
                    'annotations':{'private-unrelated':'do-not-report-private-annotation'}}
            service = {'apiVersion':'v1','kind':'Service','metadata':meta,
                       'spec':{'type':'ClusterIP','clusterIP':'10.97.0.'+str(n+10),'selector':{'app':name},
                               'ports':[{'name':'http-public','protocol':'TCP','port':23100+n,'targetPort':18000+n,'appProtocol':'http'}]}}
            endpoint = {'apiVersion':'discovery.k8s.io/v1','kind':'EndpointSlice',
              'metadata':{'name':name+'-slice','namespace':ns,'uid':'slice-uid-'+role,
                          'labels':{'kubernetes.io/service-name':name},
                          'ownerReferences':[{'apiVersion':'v1','kind':'Service','name':name,'uid':meta['uid']}]},
              'addressType':'IPv4','ports':[{'name':'http-public','protocol':'TCP','port':18000+n}],
              'endpoints':[{'addresses':['10.244.1.'+str(n+10)],'conditions':{'ready':True,'serving':True,'terminating':False},
                            'targetRef':{'kind':'Pod','namespace':ns,'name':'same-pod','uid':'pod-uid-'+role}}]}
            self.services.append(service); self.slices[(ns,name)] = [endpoint]

    def configuration(self):
        return {'current-context':self.context,'contexts':[{'name':self.context,'context':{'cluster':'cluster-renamed','user':'auth-user'}}],
                'clusters':[{'name':'cluster-renamed','cluster':{'server':self.server,'insecure-skip-tls-verify':self.insecure}}],
                'users':[{'name':'auth-user','user':{'token':'REDACTED-PRIVATE-FIXTURE'}}]}

    def response(self, path):
        self.calls.append(path)
        if self.forbidden and self.forbidden in path: raise ReadFailure('KUBERNETES_FORBIDDEN')
        parsed = urlsplit(path); parts = parsed.path.split('/')
        if parsed.path.startswith('/api/v1/namespaces/') and len(parts) == 5:
            ns = parts[-1]
            return {'apiVersion':'v1','kind':'Namespace','metadata':{'name':ns,'uid':self.uid if ns=='kube-system' else 'ns-uid-'+ns}}
        if parsed.path.endswith('/services'):
            self.services_reads += 1
            if self.services_reads == 2 and self.on_second: self.on_second()
            rows = self.services
            if '/namespaces/' in parsed.path: rows = [s for s in rows if s['metadata']['namespace']==parts[-2]]
            return {'apiVersion':'v1','kind':'ServiceList','metadata':{},'items':deepcopy(rows)}
        if parsed.path.endswith('/endpointslices'):
            ns = parts[-2]; selector = parse_qs(parsed.query)['labelSelector'][0]
            service = selector.split('=',1)[1]
            return {'apiVersion':'discovery.k8s.io/v1','kind':'EndpointSliceList','metadata':{},'items':deepcopy(self.slices[(ns,service)])}
        raise AssertionError('Unexpected API path')

    def command(self, args):
        if 'config' in args:
            assert args[-5:] == ['config','view','--minify','-o','json']
            return json.dumps(self.configuration()).encode()
        assert args[-3:-1] == ['get','--raw'] and '--insecure-skip-tls-verify=false' in args
        assert args[args.index('--server')+1] == self.server
        assert args[args.index('--context')+1] == self.context
        return json.dumps(self.response(args[-1])).encode()

    def client(self): return Kubectl(command=self.command)


def kube_metrics(http):
    http.cluster = 'observed-metric-cluster'
    original = http.response
    metrics = ('container_cpu_usage_seconds_total','container_memory_working_set_bytes')
    required = [probe_query(m+'{container!="",container!="POD",pod!="",namespace!=""}') for m in metrics]
    def response(path):
        if path.startswith('/api/v1/query?'):
            expression = parse_qs(urlsplit(path).query)['query'][0]
            if expression in required:
                rows = [] if 'kubernetes-container-labels' in http.missing else [http.sample(http.labels('containers'))]
                return {'status':'success','data':{'resultType':'vector','result':rows}}
            if expression.startswith('up{'):
                return {'status':'success','data':{'resultType':'vector','result':[http.sample(http.labels(r)) for r in http.jobs]}}
        return original(path)
    http.response = response


class KubernetesDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.objects = Objects(); self.http = HTTPFixture(); self.addCleanup(self.http.close); kube_metrics(self.http)
        self.probes = []

    def transport(self, url, **kwargs):
        self.probes.append(url)
        # Route only in the fixture: production uses the actual discovered ClusterIP.
        return Transport(self.http.url, **kwargs)

    def run_discovery(self, **kwargs):
        return discover_kubernetes(client=self.objects.client(), transport_factory=self.transport, **kwargs)

    def service(self, role='model'): return self.objects.services[list(ROLES).index(role)]
    def slice(self, role='model'):
        s = self.service(role)['metadata']; return self.objects.slices[(s['namespace'],s['name'])][0]

    def test_renamed_services_namespaces_ports_and_jobs_generate_kubernetes_proposal(self):
        r = self.run_discovery(); self.assertEqual(r['result'],'discovered',r)
        self.assertEqual(r['kubernetes']['installation'],'install-renamed')
        self.assertEqual(r['services']['model']['endpoint'],'http://10.97.0.10:23100')
        identity = r['services']['model']['identity']; self.assertEqual(identity['namespace_uid'],'ns-uid-apps-renamed')
        self.assertEqual(identity['service_uid'],'service-uid-model')
        cfg = r['connection_overlay']['resource_profile']
        self.assertEqual(cfg['environment'],'kubernetes'); self.assertEqual(cfg['container_name_label'],'container')
        self.assertEqual(cfg['sources']['containers']['job'],'runtime-new')
        self.assertEqual(cfg['scope'],{'cluster':'observed-metric-cluster'})
        self.assertEqual(len(r['metrics']['profile_validation']),7)
        self.assertEqual(r['metrics']['api_cluster_metric_label_binding'],'not_verified')
        self.assertNotIn('do-not-report-private-annotation',json.dumps(r)); self.assertNotIn('REDACTED-PRIVATE',json.dumps(r))
        self.assertTrue(all(m=='GET' and auth is None for m,path,auth in self.http.calls))
        self.assertFalse(r['activation_requested']); self.assertEqual(r['files_changed'],[])

    def test_multiple_installations_are_not_chosen_arbitrarily(self):
        other = deepcopy(self.service()); other['metadata']['uid']='other'; other['metadata']['labels'][INSTALLATION]='another'
        self.objects.services.append(other)
        r=self.run_discovery(); self.assertEqual(r['error'],'KUBERNETES_INSTALLATION_AMBIGUOUS'); self.assertFalse(self.probes)
        r=self.run_discovery(installation='install-renamed'); self.assertEqual(r['result'],'discovered')

    def test_second_installation_appearing_during_automatic_selection_blocks_overlay(self):
        def add():
            other=deepcopy(self.service()); other['metadata']['uid']='other'; other['metadata']['labels'][INSTALLATION]='another'
            self.objects.services.append(other)
        self.objects.on_second=add
        r=self.run_discovery(); self.assertEqual(r['error'],'KUBERNETES_INSTALLATION_AMBIGUOUS'); self.assertIsNone(r['connection_overlay'])

    def test_duplicate_role_is_not_probed_or_selected(self):
        s=deepcopy(self.service()); s['metadata'].update(uid='model-duplicate',name='model-duplicate')
        ep=deepcopy(self.slice()); ep['metadata'].update(name='duplicate-slice',uid='duplicate-slice')
        ep['metadata']['labels']['kubernetes.io/service-name']='model-duplicate'
        ep['metadata']['ownerReferences'][0].update(name='model-duplicate',uid='model-duplicate')
        self.objects.services.append(s); self.objects.slices[(s['metadata']['namespace'],'model-duplicate')]=[ep]
        r=self.run_discovery(); self.assertEqual(r['services']['model']['error'],'SERVICE_AMBIGUOUS')
        self.assertNotIn('http://10.97.0.10:23100',self.probes); self.assertIsNone(r['connection_overlay'])

    def test_no_labeled_installation_is_distinct_from_success(self):
        self.objects.services=[]
        r=self.run_discovery(); self.assertEqual(r['error'],'KUBERNETES_INSTALLATION_NOT_FOUND'); self.assertFalse(self.probes)

    def test_missing_role_is_reported_without_compose_fallback(self):
        self.objects.services.pop(0)
        r=self.run_discovery(); self.assertEqual(r['services']['model']['error'],'SERVICE_MISSING'); self.assertIsNone(r['connection_overlay'])

    def test_missing_role_labels_are_not_guessed_from_service_names(self):
        self.service()['metadata']['labels'].pop(ROLE)
        r=self.run_discovery(); self.assertEqual(r['error'],'KUBERNETES_SERVICE_LABELS_REQUIRED'); self.assertFalse(self.probes)

    def test_namespace_restriction_does_not_expand_silently(self):
        r=self.run_discovery(namespaces=['apps-renamed'])
        self.assertEqual(r['services']['prometheus']['error'],'SERVICE_MISSING')
        self.assertTrue(all('/api/v1/services?' not in p for p in self.objects.calls))

    def test_foreign_endpointslice_owner_is_refused(self):
        self.slice()['metadata']['ownerReferences'][0]['uid']='previous-service'
        r=self.run_discovery(); self.assertEqual(r['error'],'ENDPOINTSLICE_OWNER_MISMATCH'); self.assertFalse(self.probes)

    def test_wrong_namespace_or_service_label_is_refused(self):
        self.slice()['metadata']['namespace']='other'
        r=self.run_discovery(); self.assertEqual(r['error'],'KUBERNETES_NAMESPACE_MISMATCH')
        self.slice()['metadata']['namespace']='apps-renamed'; self.slice()['metadata']['labels']['kubernetes.io/service-name']='other'
        self.assertEqual(self.run_discovery()['error'],'ENDPOINTSLICE_SERVICE_MISMATCH')

    def test_terminating_unknown_and_nonserving_pods_are_not_ready(self):
        for conditions in ({'ready':True,'terminating':True},{},{'ready':True,'serving':False}):
            with self.subTest(conditions=conditions):
                self.slice()['endpoints'][0]['conditions']=conditions
                r=self.run_discovery(); self.assertEqual(r['services']['model']['error'],'SERVICE_READY_ENDPOINT_MISSING')

    def test_publish_not_ready_cannot_fake_readiness(self):
        self.service()['spec']['publishNotReadyAddresses']=True
        self.assertEqual(self.run_discovery()['services']['model']['error'],'SERVICE_READINESS_CONTRACT_UNSUPPORTED')

    def test_externalname_headless_and_selectorless_services_are_not_guessed(self):
        original=deepcopy(self.service()['spec'])
        for updates,expected in [({'type':'ExternalName'},'SERVICE_CLUSTER_IP_REQUIRED'),({'clusterIP':'None'},'SERVICE_CLUSTER_IP_REQUIRED'),({'selector':{}},'SERVICE_READINESS_CONTRACT_UNSUPPORTED')]:
            self.service()['spec']=dict(original,**updates)
            self.assertEqual(self.run_discovery()['services']['model']['error'],expected)

    def test_multi_port_requires_unique_http_contract_or_annotation(self):
        extra={'name':'http-metrics','port':24444,'targetPort':24444,'protocol':'TCP'}
        self.service()['spec']['ports'].append(extra)
        r=self.run_discovery(); self.assertEqual(r['services']['model']['error'],'SERVICE_HTTP_PORT_MISSING_OR_AMBIGUOUS')
        self.service()['metadata']['annotations'][PORT]='http-public'
        self.assertEqual(self.run_discovery()['result'],'discovered')

    def test_unknown_protocol_never_inferred_from_port_number(self):
        port=self.service()['spec']['ports'][0]; port.update(name='web',port=80); port.pop('appProtocol')
        self.assertEqual(self.run_discovery()['services']['model']['error'],'SERVICE_HTTP_PORT_MISSING_OR_AMBIGUOUS')

    def test_service_port_differs_from_target_port_and_named_target_is_supported(self):
        self.service()['spec']['ports'][0]['targetPort']='http-backend'
        self.assertEqual(self.run_discovery()['result'],'discovered')
        self.service()['spec']['ports'][0]['targetPort']=80
        self.assertEqual(self.run_discovery()['services']['model']['error'],'ENDPOINTSLICE_PORT_MISMATCH')

    def test_ipv6_primary_service_and_matching_endpoint_family(self):
        self.service()['spec']['clusterIP']='fd00:97::10'; self.slice()['addressType']='IPv6'
        self.slice()['endpoints'][0]['addresses']=['fd00:244::10']
        r=self.run_discovery(); self.assertEqual(r['services']['model']['endpoint'],'http://[fd00:97::10]:23100')
        self.slice()['addressType']='IPv4'; self.slice()['endpoints'][0]['addresses']=['10.244.1.10']
        self.assertEqual(self.run_discovery()['services']['model']['error'],'SERVICE_READY_ENDPOINT_MISSING')

    def test_pod_move_between_runs_keeps_service_identity_and_connection(self):
        first=self.run_discovery()
        self.slice()['endpoints'][0]['addresses']=['10.244.9.10']; self.slice()['endpoints'][0]['targetRef']['uid']='new-pod'
        second=self.run_discovery()
        self.assertEqual(first['services']['model']['identity'],second['services']['model']['identity'])
        self.assertEqual(first['connection_overlay'],second['connection_overlay'])
        self.assertNotEqual(first['deployment_fingerprint'],second['deployment_fingerprint'])

    def test_mid_discovery_pod_move_or_service_recreation_blocks_proposal(self):
        self.objects.on_second=lambda:self.slice()['endpoints'][0]['targetRef'].update(uid='new-pod')
        r=self.run_discovery(); self.assertEqual(r['error'],'KUBERNETES_BINDING_CHANGED_DURING_DISCOVERY'); self.assertIsNone(r['connection_overlay'])
        self.objects.services_reads=0
        def recreated():
            self.service()['metadata']['uid']='new-service'; self.slice()['metadata']['ownerReferences'][0]['uid']='new-service'
        self.objects.on_second=recreated
        self.assertEqual(self.run_discovery()['error'],'KUBERNETES_BINDING_CHANGED_DURING_DISCOVERY')

    def test_namespace_uid_change_blocks_proposal(self):
        self.objects.on_second=lambda:setattr(self.objects,'uid','different-cluster-namespace')
        # kube-system is read before the second services list; change at a health probe instead.
        called=False
        def transport(url,**kwargs):
            nonlocal called
            if not called:self.objects.uid='different-cluster-namespace';called=True
            return self.transport(url,**kwargs)
        r=discover_kubernetes(client=self.objects.client(),transport_factory=transport)
        self.assertEqual(r['error'],'KUBERNETES_BINDING_CHANGED_DURING_DISCOVERY')

    def test_rbac_failure_is_not_reported_as_empty_installation(self):
        self.objects.forbidden='/services'
        r=self.run_discovery(); self.assertEqual(r['error'],'KUBERNETES_FORBIDDEN'); self.assertIsNone(r['connection_overlay'])

    def test_health_failure_preserves_metadata_evidence_without_verified_endpoint(self):
        self.http.fail_health='/healthz'
        r=self.run_discovery(); self.assertEqual(r['services']['agent']['error'],'SERVICE_HEALTH_CONTRACT_MISMATCH')
        self.assertEqual(r['services']['agent']['evidence']['ready_pod_count'],1); self.assertIsNone(r['connection_overlay'])

    def test_compose_container_metrics_cannot_satisfy_kubernetes_role(self):
        self.http.missing.add('kubernetes-container-labels')
        r=self.run_discovery(); self.assertEqual(r['metrics']['roles']['containers']['status'],'not_observed'); self.assertIsNone(r['connection_overlay'])

    def test_explicit_kubernetes_profile_preserved_and_foreign_compose_profile_rejected(self):
        first=self.run_discovery(); cfg={'timeout':1,'platforms':{r:{'url':None} for r in ROLES[:4]},'resource_profile':first['connection_overlay']['resource_profile']}
        second=self.run_discovery(cfg=cfg)
        self.assertEqual(second['result'],'discovered'); self.assertNotIn('resource_profile',second['connection_overlay'])
        cfg['resource_profile']['environment']='docker-compose'
        with self.assertRaises(ReadFailure) as error:self.run_discovery(cfg=cfg)
        self.assertEqual(error.exception.code,'CONFIG_ENVIRONMENT_MISMATCH')

    def test_cli_selects_kubernetes_without_loading_or_changing_compose(self):
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder)/'missing-state'
            args=['discover.py','--provider','kubernetes','--state-dir',str(state)]
            with patch('sys.argv',args), patch('src.ops.platform.ops.discovery_kubernetes.Kubectl',return_value=self.objects.client()), \
                 patch('src.ops.platform.ops.kubernetes_client.Kubectl',return_value=self.objects.client()), \
                 patch('src.ops.platform.ops.discovery_kubernetes.Transport',self.transport), \
                 patch.object(entry.manage,'existing',side_effect=AssertionError('must not load Compose')), patch('sys.stdout',new_callable=io.StringIO) as out:
                # Capture the dispatcher contract independently of default-bound transport arguments.
                report=self.run_discovery()
                with patch('src.ops.platform.ops.discovery_kubernetes.discover_kubernetes',return_value=report):code=entry.main()
            self.assertEqual(code,0); self.assertEqual(json.loads(out.getvalue())['provider'],'kubernetes'); self.assertFalse(state.exists())

    def test_cli_detects_existing_config_drift_without_ops_api_or_state_write(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('sys.argv',['discover.py','--provider','kubernetes','--state-dir',folder]), \
                 patch.object(entry,'config_stamp',side_effect=[{'sha':'before'},{'sha':'after'}]), \
                 patch('src.ops.platform.ops.discovery_kubernetes.discover_kubernetes',return_value=self.run_discovery()), \
                 patch('sys.stdout',new_callable=io.StringIO) as output:
                self.assertEqual(entry.main(),2)
            result=json.loads(output.getvalue())
            self.assertEqual(result['error'],'CONFIG_CHANGED_DURING_DISCOVERY');self.assertIsNone(result['connection_overlay'])
            self.assertEqual(list(Path(folder).iterdir()),[])

    def test_nonbinding_annotation_change_does_not_invalidate_healthy_discovery(self):
        self.objects.on_second=lambda:self.service()['metadata']['annotations'].update(unrelated='changed')
        self.assertEqual(self.run_discovery()['result'],'discovered')

    @unittest.skipUnless(os.environ.get('PROMTOOL_TEST_BINARY'),'native promtool opt-in')
    def test_generated_kubernetes_profile_with_native_prometheus_engine(self):
        r=self.run_discovery(); cfg=r['connection_overlay']['resource_profile']
        inputs,expected=native_queries.fixture(cfg)
        cases=[{'expr':row[2],'eval_time':'5m','exp_samples':expected[key]} for key,row in queries(cfg).items()]
        signature_cases=[]
        for metric in ('container_cpu_usage_seconds_total','container_memory_working_set_bytes'):
            expression=probe_query(metric+'{container!="",container!="POD",pod!="",namespace!=""}')
            labels={**cfg['sources']['containers'],**cfg['scope'],'instance':'same:9100'}
            signature_cases.append({'expr':expression,'eval_time':'5m',
                                    'exp_samples':[{'labels':native_queries.series('',labels),'value':2}]})
        missing_namespace={'series':native_queries.series('container_memory_working_set_bytes',
                            {**labels,'container':'unattributed','pod':'present-pod'}),'values':'999+0x5'}
        doc={'rule_files':[],'evaluation_interval':'1m','tests':[
            {'interval':'1m','input_series':inputs,'promql_expr_test':cases},
            {'interval':'1m','input_series':inputs+[missing_namespace],'promql_expr_test':signature_cases}]}
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/'rules-test.json';file.write_text(json.dumps(doc))
            result=subprocess.run([os.environ['PROMTOOL_TEST_BINARY'],'test','rules',str(file)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
