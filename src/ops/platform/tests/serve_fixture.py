"""Loopback-only synthetic UI fixture; no real platforms or business credentials."""
import argparse
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[4]))
import uvicorn
from src.ops.platform.tests.test_console import Environment
from src.ops.platform.tests.test_alerts import rules_fixture, manager_fixture
from src.ops.platform.tests.test_status import server


def main():
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=17881);p.add_argument('--control',type=Path)
    a=p.parse_args();env=Environment(a.port)
    original=env.prom_routes['/api/v1/query']
    def metric(path):
        if a.control and a.control.exists():
            options=json.loads(a.control.read_text())
            env.metric_empty=options.get('empty',False)
            env.metric_label=options.get('label','fixture-container')
            if options.get('prom_failure'):return 503,{'private':'not exposed'}
        return original(path)
    env.prom_routes['/api/v1/query']=metric
    def options():return json.loads(a.control.read_text()) if a.control and a.control.exists() else {}
    def rules(path):
        opts=options()
        if opts.get('alert_rules_failure'):return 503,{}
        data=rules_fixture()
        if opts.get('alert_firing'):
            rule=next(r for r in data['data']['groups'][0]['rules'] if r['name']=='OpsHostMemoryHigh')
            item=manager_fixture();rule.update(state='firing',alerts=[{'labels':item['labels'],'state':'firing','activeAt':item['startsAt']}])
        return data
    def alerts(path):
        opts=options()
        if opts.get('alert_manager_failure'):return 503,{}
        return [manager_fixture()] if opts.get('alert_firing') else []
    env.prom_routes['/api/v1/rules']=rules
    amurl,_,_=env.stack.enter_context(server('alertmanager',{'/api/v2/alerts':alerts}))
    env.config['alertmanager_url']=amurl
    try:uvicorn.run(env.app,host='127.0.0.1',port=a.port,access_log=False,log_level='warning',proxy_headers=False)
    finally:env.close()


if __name__=='__main__':main()
