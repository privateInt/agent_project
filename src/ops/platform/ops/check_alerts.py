#!/usr/bin/env python3
"""Read only the new O04 Ops endpoint; never inject a production alert or inference."""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[4]))
from src.ops.platform.contracts import ReadFailure, require
from src.ops.platform.settings import load
from src.ops.platform.transport import Transport
from src.ops.platform.alerts import RULES


def check(cfg):
    url=cfg['public_url'] if cfg['tls_cert'] else 'http://127.0.0.1:'+str(cfg['port'])
    api=Transport(url,cfg['access_file'],timeout=30)
    _,data=api.get('/api/alerts')
    sources=data.get('sources',{}); prom=sources.get('prometheus',{}); am=sources.get('alertmanager',{})
    rules=(prom.get('data') or {}).get('items',[])
    good=data.get('status')=='ok' and prom.get('status')=='ok' and am.get('status')=='ok'
    good=good and len(rules)==len(RULES) and {r['id'] for r in rules}==set(RULES) and all(r['health']=='ok' for r in rules)
    return {'stage':'O04-ops-alerts-readback','result':'passed' if good else 'attention_required',
            'checked_at':data.get('checked_at'),'rules':[{k:r[k] for k in ('id','state','health','last_evaluation')} for r in rules],
            'sources':{k:{'status':v.get('status'),'error':v.get('error')} for k,v in sources.items()},
            'active_rule_alerts':data.get('active_count'),'alertmanager_active_count':(am.get('data') or {}).get('count'),
            'external_delivery':data.get('external_delivery'),'scope':'readback_not_fault_injection_or_business_quality',
            'production_alerts_posted':0,'business_api_requests':0,'inference_requested':False,
            'browser_acceptance':'deferred_by_user'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path.home()/'.local/state/agent-project-ops/server/host-config.json')
    args=parser.parse_args(); report=check(load(args.config))
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['result']=='passed' else 2


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ReadFailure,OSError,ValueError,KeyError,TypeError) as exc:
        print(json.dumps({'result':'error','code':exc.code if isinstance(exc,ReadFailure) else 'LOCAL_CONFIG_OR_RESPONSE_ERROR'}))
        raise SystemExit(1)
