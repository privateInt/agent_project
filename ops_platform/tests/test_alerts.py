"""O04 API/auth/failure recovery with actual loopback HTTP fixtures."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import time
import unittest

from ops_platform.alerts import Alerts, RULES, SCOPE
from ops_platform.server import create_app
from ops_platform.service import ReadService
from ops_platform.settings import load
from ops_platform.tests.test_console import Environment, OPS_KEY
from ops_platform.tests.test_status import server, PRIVATE, ID


def rules_fixture():
    return {'status':'success','data':{'groups':[{'name':SCOPE,'rules':[
        {'name':name,'type':'alerting','duration':seconds,'labels':{'scope':SCOPE,'severity':'warning'},
         'state':'inactive','health':'ok','alerts':[], 'lastEvaluation':datetime.now(timezone.utc).isoformat(),
         'query':PRIVATE,'lastError':PRIVATE} for name,(_,seconds) in RULES.items()]}]}}


def manager_fixture():
    return {'labels':{'alertname':'OpsHostMemoryHigh','scope':SCOPE,'severity':'warning',
                     'job':'host','instance':'127.0.0.1:16311','private':PRIVATE},
            'fingerprint':'a'*16,'startsAt':datetime.now(timezone.utc).isoformat(),
            'endsAt':(datetime.now(timezone.utc)+timedelta(minutes=3)).isoformat(),
            'annotations':{'secret':PRIVATE}, 'generatorURL':'http://private/'+PRIVATE,
            'status':{'state':'active','silencedBy':[],'inhibitedBy':[]}}


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.env=Environment();self.addCleanup(self.env.close)
        self.rules=rules_fixture(); self.manager=[]
        self.env.prom_routes['/api/v1/rules']=lambda _:self.rules
        url,self.routes,self.calls=self.env.stack.enter_context(server('alertmanager',{'/api/v2/alerts':lambda _:self.manager}))
        self.env.config['alertmanager_url']=url
        self.client=self.env.client();self.auth={'Authorization':'Bearer '+OPS_KEY}

    def get(self): return self.client.get('/api/alerts',headers=self.auth).json()
    def rule(self,name): return next(r for r in self.rules['data']['groups'][0]['rules'] if r['name']==name)

    def test_empty_is_healthy_only_when_both_sources_and_all_rules_are_valid(self):
        result=self.get()
        self.assertEqual(result['status'],'ok');self.assertEqual(result['active_count'],0)
        self.assertEqual(len(result['sources']['prometheus']['data']['items']),8)
        self.assertNotIn(PRIVATE,json.dumps(result))
        self.assertTrue(all(m=='GET' and auth is None for m,_,auth in self.calls))
        self.assertTrue(all(m=='GET' and auth is None for m,_,auth in self.env.prom_calls))
        self.assertTrue(all(not c for c in self.env.calls.values()))

    def test_auth_query_and_mutation_boundaries(self):
        self.assertEqual(self.client.get('/api/alerts').status_code,401)
        self.assertEqual(self.client.get('/api/alerts?url=http://bad',headers=self.auth).status_code,400)
        for method in ('post','put','patch','delete'):
            self.assertEqual(getattr(self.client,method)('/api/alerts',headers=self.auth).status_code,405)
        self.assertEqual(self.calls,[])

    def test_pending_firing_and_resolution_are_source_states(self):
        rule=self.rule('OpsHostMemoryHigh'); alert=manager_fixture()
        for state in ('pending','firing'):
            rule['state']=state;rule['alerts']=[{'labels':alert['labels'],'state':state,'activeAt':alert['startsAt']}]
            self.manager=[] if state=='pending' else [alert]
            result=self.get();self.assertEqual(result['active_count'],1)
            row=next(r for r in result['sources']['prometheus']['data']['items'] if r['id']==rule['name'])
            self.assertEqual(row['alerts'][0]['state'],state)
            self.assertNotIn(PRIVATE,json.dumps(result))
        rule['state']='inactive';rule['alerts']=[];self.manager=[]
        self.assertEqual(self.get()['active_count'],0)
        self.assertEqual(self.get()['history'],'current_state_only')

    def test_http_errors_invalid_json_and_recovery_preserve_success_timestamp(self):
        first=self.get()['sources']['alertmanager']['last_success_at']
        for reply,code in (((503,{'error':PRIVATE}),'UPSTREAM_HTTP_ERROR'),
                           ((401,{'error':PRIVATE}),'UNAUTHENTICATED'),(b'no-json','INVALID_RESPONSE'),({},'INVALID_RESPONSE')):
            self.routes['/api/v2/alerts']=reply
            result=self.get();source=result['sources']['alertmanager']
            self.assertEqual(result['status'],'partial');self.assertIsNone(result['active_count'])
            self.assertEqual(source['error']['code'],code);self.assertIsNone(source['data'])
            self.assertEqual(source['last_success_at'],first)
            self.assertNotIn(PRIVATE,json.dumps(result))
        self.routes['/api/v2/alerts']=[]
        result=self.get();self.assertEqual(result['status'],'ok')
        self.assertGreaterEqual(result['sources']['alertmanager']['last_success_at'],first)

    def test_missing_duplicate_stale_or_failed_rules_are_not_empty_healthy(self):
        self.rules['data']['groups']=[]
        self.assertEqual(self.get()['status'],'partial')
        self.rules=rules_fixture(); rows=self.rules['data']['groups'][0]['rules']
        rows[-1]=deepcopy(rows[0]);self.assertEqual(self.get()['status'],'partial')
        self.rules=rules_fixture(); self.rule('OpsTargetDown')['lastEvaluation']='2026-01-01T00:00:00Z'
        result=self.get();self.assertEqual(result['status'],'partial');self.assertIsNone(result['active_count'])
        self.rules=rules_fixture();self.rule('OpsTargetDown')['health']='err'
        result=self.get();self.assertEqual(result['status'],'partial');self.assertNotIn(PRIVATE,json.dumps(result))

    def test_alertmanager_bounds_scope_duplicate_and_suppressed_state(self):
        row=manager_fixture(); self.manager=[row]
        self.assertEqual(self.get()['sources']['alertmanager']['data']['count'],1)
        row['status'].update(state='suppressed',silencedBy=['synthetic'])
        self.assertTrue(self.get()['sources']['alertmanager']['data']['items'][0]['silenced'])
        self.manager=[row,row];self.assertEqual(self.get()['sources']['alertmanager']['error']['code'],'DUPLICATE_IDENTITY')
        self.manager=[row]*501;self.assertEqual(self.get()['sources']['alertmanager']['error']['code'],'RESPONSE_TOO_LARGE')
        self.manager=[row];row['labels']['scope']='foreign'
        self.assertEqual(self.get()['sources']['alertmanager']['error']['code'],'ALERT_SCOPE_MISMATCH')

    def test_timestamp_cache_survives_service_restart_and_failure(self):
        first=self.get()['sources']['alertmanager']['last_success_at']
        self.routes['/api/v2/alerts']=(503,{})
        read=ReadService(self.env.config)
        try:self.assertEqual(read.alerts()['sources']['alertmanager']['last_success_at'],first)
        finally:read.close()

    def test_original_business_failure_remains_original_after_alert_error(self):
        self.routes['/api/v2/alerts']=(503,{})
        self.assertEqual(self.get()['status'],'partial')
        business=self.client.get('/api/platforms/agent/agent-runs/'+ID,headers=self.auth).json()
        self.assertEqual(business['data']['state'],'failed')
        self.assertEqual(business['data']['error']['code'],'ANSWER_REFERENCE_INVALID')

    def test_old_config_remains_byte_compatible_optional_endpoint_is_validated(self):
        path=self.env.tmp/'config.json';cfg=deepcopy(self.env.config);cfg.pop('alertmanager_url')
        path.write_text(json.dumps(cfg));self.assertEqual(load(path),cfg)
        cfg['alertmanager_url']='http://127.0.0.1:16330';path.write_text(json.dumps(cfg))
        self.assertEqual(load(path),cfg)
        cfg['alertmanager_url']='http://user:secret@host';path.write_text(json.dumps(cfg))
        with self.assertRaises(Exception):load(path)

    def test_rule_contract_matches_deployed_configuration(self):
        path=Path(__file__).resolve().parents[2]/'infra/observability/alerts/o04.rules.yml'
        rules=json.loads(path.read_text())['groups'][0]['rules']
        self.assertEqual({r['alert']:int(r['for'][:-1]) for r in rules},{k:v[1] for k,v in RULES.items()})


if __name__=='__main__':unittest.main()
