#!/usr/bin/env python3
"""GET-only O03 API readback. Existing failed business records remain failed."""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from ops_platform.contracts import ReadFailure, identifier
from ops_platform.settings import load
from ops_platform.transport import Transport


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=Path.home()/'.local/state/agent-project-ops/server/host-config.json')
    p.add_argument('--agent-run-id',required=True)
    a=p.parse_args();identifier(a.agent_run_id);cfg=load(a.config)
    # Loopback default keeps the Ops credential off the LAN during command-line checks.
    url=cfg['public_url'] if cfg['tls_cert'] else 'http://127.0.0.1:'+str(cfg['port'])
    api=Transport(url,cfg['access_file'],timeout=30)
    result={'stage':'O03-ops-console-readback','method':'GET','inference_requested':False,'remote_business_mutations_requested':False}
    for key,path in [('overview','/api/overview'),('resources','/api/resources'),
        ('agent','/api/platforms/agent/agent-runs/'+a.agent_run_id),('trace','/api/agent-runs/'+a.agent_run_id+'/trace')]:
        try:
            _,data=api.get(path)
            if key=='overview':
                result[key]={name:{'status':v['status'],'gateway':v.get('gateway',{}).get('status'),
                    'readiness':v.get('readiness',{}).get('data') or v.get('readiness',{}).get('status')} for name,v in data['platforms'].items()}
            elif key=='resources':
                result[key]={name:{'status':v['status'],'count':(v.get('data') or {}).get('count'),
                    'error':v.get('error'),'coverage':(v.get('data') or {}).get('coverage'),
                    'resource_schema':(v.get('data') or {}).get('resource_schema'),
                    'environment':(v.get('data') or {}).get('environment'),
                    'measurement_scope':(v.get('data') or {}).get('measurement_scope'),
                    'unavailable_values':sum(row['value'] is None for row in (v.get('data') or {}).get('items',[])),
                    'down_targets':sum(row['value']!=1 for row in (v.get('data') or {}).get('items',[])) if name=='targets' else None}
                    for name,v in data['metrics'].items()}
            elif key=='agent':
                d=data.get('data') or {};result[key]={'status':data['status'],'id':d.get('id'),'state':d.get('state'),
                    'identifiers':d.get('identifiers'),'business_error':d.get('error'),'query_error':data.get('error')}
            else:
                d=data.get('data') or {};result[key]={'status':data['status'],'trace_id':d.get('trace_id'),'relationship':d.get('relationship'),
                    'span_count':len(d.get('items',[])),'missing_spans':d.get('missing_spans'),'error':data.get('error')}
        except (ReadFailure,KeyError,TypeError) as exc:
            result[key]={'status':'error','error':{'code':exc.code if isinstance(exc,ReadFailure) else 'INVALID_RESPONSE'}}
    overview=result['overview'];metrics=result['resources']
    good='error' not in overview and all(v['status']=='ok' and v['gateway']=='ok' and
        ((isinstance(v['readiness'],dict) and v['readiness'].get('ready') is True)
         or (name!='agent' and v['readiness']=='unsupported')) for name,v in overview.items())
    good=good and 'error' not in metrics and all(v['status']=='ok' and (v['count'] or 0)>0
        and v['unavailable_values']==0 and not v['down_targets'] for v in metrics.values())
    good=good and result['agent']['status']=='ok' and result['trace']['status']=='ok'
    result['result']='passed' if good else 'attention_required'
    result['scope']='new Ops API readback; browser and Docker restart acceptance separate'
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if good else 2


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ReadFailure,OSError,ValueError) as exc:
        print(json.dumps({'result':'error','code':exc.code if isinstance(exc,ReadFailure) else 'LOCAL_CONFIG_ERROR'}));raise SystemExit(1)
