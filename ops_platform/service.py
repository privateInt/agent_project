"""Bounded read aggregation. Separate upstream errors and original business outcomes."""
from concurrent.futures import ThreadPoolExecutor
import threading

from .client import now, new_client
from .contracts import RESOURCES, ReadFailure
from .observations import Prometheus, Langfuse, QUERIES
from .state import success_cache
from .alerts import Alerts, DEFAULT_URL


def failure(code):
    return {'status':'error','data':None,'error':{'code':code},'checked_at':now(),
            'observed_at':None,'last_success_at':None,'freshness':'unavailable'}


class ReadService:
    def __init__(self,config):
        self.config=config
        self.lock=threading.Lock()
        self.pool=ThreadPoolExecutor(max_workers=4,thread_name_prefix='ops-read')
        with success_cache(config['state_file']) as times: self.times=dict(times)

    def close(self):
        self.pool.shutdown(wait=True,cancel_futures=True)

    def read(self,fn):
        with self.lock: times=dict(self.times)
        try: result=fn(times)
        except ReadFailure as exc: result=failure(exc.code)
        except (ValueError,TypeError,KeyError,OverflowError,OSError): result=failure('INVALID_RESPONSE')
        with self.lock:
            changed={k:v for k,v in times.items() if v>self.times.get(k,'')}
            if changed:
                self.times.update(changed)
                self.times=dict(sorted(self.times.items(),key=lambda p:p[1],reverse=True)[:1000])
                try:
                    with success_cache(self.config['state_file']) as stored: stored.update(self.times)
                except (ReadFailure,OSError): result['timestamp_persistence']='unavailable'
        return result

    def client(self,name,times):
        return new_client(name,self.config['platforms'],self.config['timeout'],times)

    def overview(self):
        def platform(name):
            return name, self.read(lambda times:{'status':'ok','gateway':self.client(name,times).health(),
                                                 'readiness':self.client(name,times).readiness()})
        return {'checked_at':now(),'platforms':dict(self.pool.map(platform,RESOURCES)),
                'global_counts_supported':False,'quality':'not_assessed'}

    def page(self,platform,resource,limit,cursor,parent_id):
        return self.read(lambda times:self.client(platform,times).page(resource,limit,cursor,parent_id))

    def detail(self,platform,resource,ident):
        return self.read(lambda times:self.client(platform,times).detail(resource,ident))

    def resources(self):
        def metric(key):
            return key,self.read(lambda times:Prometheus(self.config['prometheus_url'],times,self.config['timeout'],self.config.get('resource_profile')).metric(key))
        return {'checked_at':now(),'metrics':dict(self.pool.map(metric,QUERIES)),
                'scope':'current_resource_samples; no model attribution or global task counts'}

    def trace(self,ident):
        detail=self.detail('agent','agent-runs',ident)
        return self.read(lambda times:Langfuse(self.config['langfuse_url'],self.config['langfuse_file'],times,
                                             self.config['timeout']).trace(detail))

    def alerts(self):
        def source(name):
            url = self.config['prometheus_url'] if name == 'prometheus' else self.config.get('alertmanager_url', DEFAULT_URL)
            return name, self.read(lambda times: (Alerts(name,url,times,self.config['timeout']).rules()
                if name == 'prometheus' else Alerts(name,url,times,self.config['timeout']).active()))
        sources = dict(self.pool.map(source, ('prometheus','alertmanager')))
        complete = all(s['status']=='ok' for s in sources.values())
        return {'status':'ok' if complete else 'partial', 'checked_at':now(), 'sources':sources,
                'active_count':sources['prometheus']['data']['active_count'] if complete else None,
                'scope':'infrastructure_alerts', 'history':'current_state_only',
                'external_delivery':'not_configured_by_this_release', 'business_quality':'not_assessed'}
