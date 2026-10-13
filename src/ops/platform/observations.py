"""Profiled Prometheus queries and identity-checked O02 Langfuse observations (GET only)."""
import base64
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import urllib.parse

from .client import Client
from .contracts import ReadFailure, require, machine, timestamp, identifier
from .transport import Transport, strict_json


def safe_label(value):
    require(isinstance(value, str) and len(value) <= 256 and value.isprintable())
    return value



from .resource_profiles import (DEFAULT_QUERIES as QUERIES, METRIC_UNITS, PUBLIC_LABELS,
                                profile, queries, resource_identity)


class Prometheus(Client):
    def __init__(self, url, times, timeout=5, resource_profile=None):
        self.profile = profile(resource_profile)
        self.queries = queries(self.profile)
        super().__init__('model', url, timeout=timeout, success_times=times)
        self.platform = 'prometheus'
        self.binding = hashlib.sha256(('prometheus|'+url+'|'+json.dumps(self.profile,sort_keys=True)).encode()).hexdigest()

    def metric(self, key):
        require(key in self.queries, 'UNSUPPORTED_METRIC')
        label, unit, expression = self.queries[key]
        def convert(raw, status):
            require(raw.get('status') == 'success' and isinstance(raw.get('data'), dict))
            data = raw['data']
            require(data.get('resultType') == 'vector' and isinstance(data.get('result'), list))
            require(len(data['result']) <= 501, 'RESPONSE_TOO_LARGE')
            rows = []
            for item in data['result'][:500]:
                require(isinstance(item, dict) and isinstance(item.get('metric'), dict))
                pair = item.get('value')
                require(isinstance(pair, list) and len(pair) == 2 and type(pair[0]) in (int,float) and math.isfinite(pair[0]))
                require(isinstance(pair[1], str) and len(pair[1]) <= 128)
                try: value = float(pair[1])
                except ValueError: raise ReadFailure('INVALID_RESPONSE') from None
                labels = {k:safe_label(v) for k,v in item['metric'].items()
                          if k in PUBLIC_LABELS}
                valid = math.isfinite(value) and abs(value) < 1e18
                rows.append({'labels':labels, 'resource':resource_identity(key,labels,self.profile),
                             'value':value if valid else None,
                             'value_status':'observed' if valid else 'unavailable',
                             'evaluated_at':datetime.fromtimestamp(pair[0],timezone.utc).isoformat().replace('+00:00','Z'),
                             'unit':METRIC_UNITS.get(labels.get('__name__'),unit)})
            return {'key':key,'label':label,'unit':unit,'items':rows,'count':len(rows),
                    'coverage':'truncated' if len(data['result']) > 500 else 'query_result',
                    'resource_schema':1,'environment':self.profile['environment'],
                    'measurement_scope': 'node_available_based' if key=='host_memory' else 'container_working_set' if key=='container_memory' else 'source_metric',
                    'freshness_policy':'source age under 90s; resource values require exporter up',
                    'warnings_present':bool(raw.get('warnings') or raw.get('infos')),
                    'empty_reason':'no_fresh_series_or_exporter_unavailable' if not rows else None}
        result = self._read('/api/v1/query?'+urllib.parse.urlencode({'query':expression,'timeout':'4s','limit':501}), convert)
        # Fixed expressions are internal details, not browser-controlled or exposed URLs.
        result.pop('source_path',None)
        if result['status']=='ok' and (result['data']['coverage']=='truncated' or result['data']['warnings_present']):
            result['status']='partial'
        return result


def read_lf_credentials(path):
    try:
        p=Path(path)
        require(not p.is_symlink() and p.is_file() and p.stat().st_size <= 16384, 'INVALID_CREDENTIAL_FILE')
        raw=strict_json(p.read_bytes())
        require(isinstance(raw,dict), 'INVALID_CREDENTIAL_FILE')
        for key in ('project_id','public_key','secret_key'):
            require(isinstance(raw.get(key),str) and re.fullmatch('[A-Za-z0-9_-]{8,256}',raw[key]), 'INVALID_CREDENTIAL_FILE')
        return {key:raw[key] for key in ('project_id','public_key','secret_key')}
    except (OSError,UnicodeError):
        raise ReadFailure('CREDENTIAL_UNAVAILABLE') from None


class LangfuseTransport(Transport):
    def headers(self,path):
        require(path.startswith('/api/public/v2/observations?'), 'INVALID_PATH')
        credentials=read_lf_credentials(self.token_file)
        auth=base64.b64encode((credentials['public_key']+':'+credentials['secret_key']).encode()).decode()
        return {'Accept':'application/json','Accept-Encoding':'identity','Connection':'close','Authorization':'Basic '+auth}


def trace_id(run_id):
    identifier(run_id)
    return hashlib.sha256(('agent-platform.o02:agent:'+run_id).encode()).hexdigest()[:32]


def span_id(trace,key):
    return hashlib.sha256((trace+':'+key).encode()).hexdigest()[:16]


class Langfuse(Client):
    def __init__(self,url,credential_file,times,timeout=5):
        super().__init__('model',url,timeout=timeout,success_times=times)
        self.transport=LangfuseTransport(url,token_file=credential_file,timeout=timeout)
        self.platform='langfuse'
        self.binding=hashlib.sha256(('langfuse|'+url+'|'+str(credential_file)).encode()).hexdigest()

    def trace(self,detail):
        require(detail.get('status') in ('ok','partial') and detail.get('data'), 'AGENT_RECORD_UNAVAILABLE')
        run=detail['data']; related=detail.get('related',{})
        require(related.get('steps',{}).get('status')=='ok','AGENT_STEPS_UNAVAILABLE')
        steps=related['steps']['data']; ident=run['id']; trace=trace_id(ident)
        bounds=[s[k] for s in steps for k in ('started_at','completed_at') if s.get(k)]
        require(bounds,'SOURCE_TIME_UNAVAILABLE')
        dates=[datetime.fromisoformat(x.replace('Z','+00:00')) for x in bounds]
        start=min(dates)-timedelta(seconds=60);end=max(dates)+timedelta(seconds=60)
        require(end-start <= timedelta(days=14),'TRACE_WINDOW_TOO_LARGE')
        expected={'agent.execute':('',None,run['state'],run['identifiers']['request_id'])}
        expected.update({'step.'+s['step_id']:('', 'agent.execute',s['state'],run['identifiers']['request_id']) for s in steps})
        call=None
        if run['identifiers'].get('tool_call_id'):
            require(related.get('tool_call',{}).get('status')=='ok','TOOL_RECORD_UNAVAILABLE')
            call=related['tool_call']['data'];prefix='tool:'+call['id']+':'
            expected['tool.execute']=(prefix,'step.search',call['state'],call['identifiers']['request_id'])
            if call['state']=='succeeded':
                for name in ('model.embedding','knowledge.search'):
                    expected[name]=(prefix,'tool.execute','succeeded',call['identifiers']['request_id'])
        if next(s for s in steps if s['step_id']=='generate')['state']=='succeeded':
            expected['model.chat']=('', 'step.generate','succeeded',run['identifiers']['request_id'])
        credentials=read_lf_credentials(self.transport.token_file)
        self.binding=hashlib.sha256((self.binding+'|'+credentials['project_id']).encode()).hexdigest()
        def convert(raw,status):
            items=raw.get('data');meta=raw.get('meta') or {}
            require(isinstance(items,list) and len(items)<=32 and isinstance(meta,dict))
            seen=set();names=set();rows=[]
            for row in items:
                require(isinstance(row,dict) and isinstance(row.get('metadata'),dict))
                name=row.get('name');rm=row['metadata']
                require(name in expected or name in ('model.chat','model.embedding','knowledge.search'),'UNEXPECTED_OBSERVATION')
                require(name not in names and row.get('id') not in seen,'DUPLICATE_IDENTITY')
                names.add(name);seen.add(row.get('id'))
                require(row.get('traceId')==trace and row.get('projectId')==credentials['project_id'],'TRACE_IDENTITY_MISMATCH')
                require(rm.get('agent_run_id')==ident and rm.get('scope')=='runtime_execution'
                        and row.get('environment')=='agent-platform','TRACE_IDENTITY_MISMATCH')
                prefix,parent,state,request_id=expected.get(name,('', 'step.generate',None,run['identifiers']['request_id']))
                if name in ('model.embedding','knowledge.search'):
                    require(call is not None,'TOOL_RECORD_UNAVAILABLE')
                    prefix='tool:'+call['id']+':';parent='tool.execute';request_id=call['identifiers']['request_id']
                parent_prefix=('tool:'+call['id']+':') if parent=='tool.execute' else ''
                require(row.get('id')==span_id(trace,prefix+name) and (row.get('parentObservationId') or None)==(span_id(trace,parent_prefix+parent) if parent else None),'TRACE_PARENT_MISMATCH')
                require(rm.get('request_id')==request_id,'TRACE_REQUEST_MISMATCH')
                if name in ('tool.execute','model.embedding','knowledge.search'):
                    require(rm.get('tool_call_id')==call['id'] and rm.get('tool_version_id')==call['identifiers']['tool_version_id'],'TRACE_VERSION_MISMATCH')
                else:
                    require(rm.get('agent_version_id')==run['identifiers']['agent_version_id'],'TRACE_VERSION_MISMATCH')
                if state not in (None,'accepted','running'):
                    require(rm.get('state')==state,'TRACE_STATE_MISMATCH')
                    require(row.get('level')==('ERROR' if state in ('failed','interrupted') else 'DEFAULT'),'TRACE_LEVEL_MISMATCH')
                expected_type={'agent.execute':'AGENT','tool.execute':'TOOL','model.chat':'GENERATION',
                               'model.embedding':'EMBEDDING','knowledge.search':'RETRIEVER'}.get(name,'SPAN')
                require(row.get('type')==expected_type,'TRACE_TYPE_MISMATCH')
                if name=='agent.execute' and run['error'].get('code'):
                    require(rm.get('error_code')==run['error']['code'],'TRACE_ERROR_MISMATCH')
                started=timestamp(row.get('startTime'));ended=timestamp(row.get('endTime'))
                require(started and ended)
                a=datetime.fromisoformat(started.replace('Z','+00:00'));b=datetime.fromisoformat(ended.replace('Z','+00:00'))
                require(start<=a<=b<=end,'TRACE_TIME_MISMATCH')
                counts={}
                for key in ('prompt_tokens','completion_tokens','total_tokens','returned_count','selected_count'):
                    if key in rm:
                        v=str(rm[key]);require(re.fullmatch('[0-9]{1,10}',v) and int(v)<=10**9)
                        counts[key]=int(v)
                rows.append({'id':row['id'],'name':name,'parent_id':row.get('parentObservationId'),
                    'state':machine(rm.get('state')),'level':machine(row.get('level')),
                    'started_at':started,'completed_at':ended,'duration_ms':round((b-a).total_seconds()*1000,3),
                    'error_code':machine(rm.get('error_code')),'counts':counts})
            return {'agent_run_id':ident,'trace_id':trace,'project_id':credentials['project_id'],
                'relationship':'verified_records' if rows else 'not_observed','items':sorted(rows,key=lambda r:(r['started_at'],r['name'])),
                'missing_spans':sorted(set(expected)-names),'has_more':bool(meta.get('cursor')),
                'business_state':run['state'],'quality':'not_assessed','content_excluded':True}
        path='/api/public/v2/observations?'+urllib.parse.urlencode({'traceId':trace,'limit':32,
            'fields':'core,basic,metadata','fromStartTime':start.isoformat(),'toStartTime':end.isoformat()})
        result=self._read(path,convert); result.pop('source_path',None)
        if result['status']=='ok':
            data=result['data']
            if not data['items']: result['status']='not_observed'
            elif data['has_more'] or data['missing_spans']: result['status']='partial'
        return result
