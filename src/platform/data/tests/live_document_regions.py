#!/usr/bin/env python3
"""Compare three source-pixel inputs through the normal upload/refinement APIs.

Uses an existing ready OCR deployment. No model deployment, direct inference,
original/result replacement or deletion. Resume the printed report after a lost
reply; new invocations intentionally create new experiments. --evaluate is offline.
"""
import argparse
import copy
import fcntl
import hashlib
import json
from pathlib import Path
import time
import urllib.parse
import uuid

from live_collection_smoke import API, expect, save
from live_upload_smoke import make_report as upload_report, checked_source, upload, check_document
from live_refinement_smoke import verify_result
from live_ocr_quality import PIN_FIELDS
from document_regions import PLATFORM, PLAN, REFERENCE, plan_and_reference, render_inputs, score_case

VERSION = 'document-regions-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def code_identity():
    files=[Path(__file__), Path(__file__).with_name('document_regions.py'),
           Path(__file__).with_name('audit_document_quality.py'), PLAN, REFERENCE,
           PLATFORM/'jobs/refinement/parse/ocr.py']
    files.extend(Path(__file__).with_name(name+'.py') for name in
                 ('live_refinement_smoke','live_upload_smoke','live_collection_smoke','live_ocr_quality','ocr_quality'))
    return digest({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files})


def get(api, path):
    code, value=api.call(path); expect(code,200,value); return value


def download(api,path):
    code,raw,_=api.call(path,binary=True,timeout=90); expect(code,200,'download'); return raw


def ready(model, identity):
    dep=get(model,'/api/deployments/'+identity['deployment_id'])
    if (dep.get('status')!='ready' or dep.get('runtime_state')!='ready' or dep.get('desired_state')!='running'
            or dep.get('serving',{}).get('profile')!='surya-ocr-v2' or dep.get('artifact_kind')!='snapshot'
            or any(dep.get(k)!=identity[k] for k in PIN_FIELDS)):
        raise ValueError('Original OCR deployment/runtime is not ready or changed; no replacement model requested')


def baseline_run(api,source_id,deployment_id,run_id=None):
    if run_id:
        run=get(api,'/api/refinement-runs/'+str(uuid.UUID(run_id)))
        if run.get('source_document_id')!=source_id or (run.get('ocr') or {}).get('deployment_id')!=deployment_id:
            raise ValueError('Selected baseline source/model mismatch')
        return run
    cursor=''; seen=set()
    for _ in range(10):
        page=get(api,'/api/refinement-runs?'+urllib.parse.urlencode({'limit':100,'cursor':cursor}))
        for item in page['items']:
            if item.get('source_document_id')==source_id and item.get('output_document_id'):
                run=get(api,'/api/refinement-runs/'+item['refinement_run_id'])
                if run.get('status')=='succeeded' and (run.get('ocr') or {}).get('deployment_id')==deployment_id:
                    return run
        cursor=page.get('next_cursor','')
        if not cursor: break
        if cursor in seen: raise ValueError('Repeated listing cursor')
        seen.add(cursor)
    raise ValueError('No completed OCR baseline in the latest 1000 runs; provide --baseline-run-id')


def initialize(api,model,source_id,deployment_id,root,run_id=None):
    source_id, deployment_id = str(uuid.UUID(source_id)), str(uuid.UUID(deployment_id))
    source=get(api,'/api/source-documents/'+source_id)
    data=download(api,'/api/source-documents/'+source_id+'/content')
    plan,_=plan_and_reference()
    if source.get('state')!='ready' or source.get('sha256')!=hashlib.sha256(data).hexdigest() or source.get('size_bytes')!=len(data) or source['sha256']!=plan['source_sha256']:
        raise ValueError('Source does not match the authored sample; no requests submitted')
    run=baseline_run(api,source_id,deployment_id,run_id)
    meta=get(api,'/api/canonical-documents/'+run['output_document_id'])
    raw=download(api,'/api/canonical-documents/'+meta['document_id']+'/content')
    baseline=verify_result(source,run,meta,raw)
    ready(model,baseline['ocr']['identity'])
    key=str(uuid.uuid4()); path=root.resolve()/key/'report.json'
    inputs=render_inputs(data,baseline,path.parent/'inputs')
    (path.parent/'baseline.json').write_bytes(raw)
    uploads=upload_report(api,[path.parent/'inputs'/i['file'] for i in inputs])['uploads']
    cases=[]
    for item,up in zip(inputs,uploads):
        up['name']='region-experiment-'+key+'-'+item['id']
        request={'name':'region-experiment-'+key+'-'+item['id'],'rules_version':'basic-cleaning-v1','ocr_deployment_id':deployment_id}
        seed={'input':item,'upload':up,'key':str(uuid.uuid4()),'request':request}
        cases.append({'id':item['id'],'seed':seed,'seed_sha256':digest(seed),'state':'pending'})
    report={'version':VERSION,'key':key,'code_identity':code_identity(),'api':api.base,'model_api':model.base,
            'source':source,'baseline_run':run,'baseline_metadata':meta,'baseline':baseline,
            'identity':baseline['ocr']['identity'],'cases':cases,'complete':False,'all_probes_passed':False,
            'scope':plan['scope'],'production_result_modified':False}
    report['fixed_sha256']=fixed_digest(report)
    save(path,report)
    return report,path


def fixed_digest(report):
    return digest({k:report[k] for k in ('version','key','code_identity','api','model_api','source',
                  'baseline_run','baseline_metadata','baseline','identity','scope')})


def validate(report,path):
    if report.get('version')!=VERSION or report.get('code_identity')!=code_identity() or report.get('fixed_sha256')!=fixed_digest(report):
        raise ValueError('Experiment code or recorded inputs changed; no requests submitted')
    plan,_=plan_and_reference()
    if [c['id'] for c in report['cases']] != [c['id'] for c in plan['cases']]:
        raise ValueError('Region cases changed')
    raw=(path.parent/'baseline.json').read_bytes()
    if verify_result(report['source'],report['baseline_run'],report['baseline_metadata'],raw)!=report['baseline']:
        raise ValueError('Baseline cache changed')
    if report['baseline']['ocr']['identity']!=report['identity']:
        raise ValueError('Baseline OCR identity changed')
    for case in report['cases']:
        seed=case['seed']; item=seed['input']
        if case.get('seed_sha256')!=digest(seed) or item['id']!=case['id']:
            raise ValueError('Region request changed')
        if Path(seed['upload']['path']).resolve()!=(path.parent/'inputs'/item['file']).resolve():
            raise ValueError('Region input path changed')
        data=checked_source(seed['upload'])
        if hashlib.sha256(data).hexdigest()!=item['sha256'] or len(data)!=item['size_bytes']:
            raise ValueError('Region pixels changed')
        if case.get('source'):
            check_document(seed['upload'],case['source'],data)
        if case['state'] not in ('pending','evaluated','failed'):
            raise ValueError('Unknown case state')
        if case['state']=='evaluated':
            content=(path.parent/(case['id']+'.json')).read_bytes()
            doc=verified_case(report,case,content)
            if doc!=case.get('document'): raise ValueError('Canonical report/cache differs')
            case['evaluation']=score_case(case['id'],doc,report['baseline'])


def verified_case(report,case,content):
    doc=verify_result(case['source'],case['run'],case['metadata'],content)
    if doc.get('ocr',{}).get('identity')!=report['identity']:
        raise ValueError('Candidate model/runtime differs from baseline')
    seed=case['seed']
    if case['source']['sha256']!=seed['input']['sha256'] or case['source']['size_bytes']!=seed['input']['size_bytes']:
        raise ValueError('Candidate source differs from prepared pixels')
    if report.get('processor_image_id') and doc['processor']['image_id']!=report['processor_image_id']:
        raise ValueError('Processor changed within experiment; do not mix results')
    return doc


def finish(report,path):
    report['complete']=all(c['state'] in ('evaluated','failed') for c in report['cases'])
    report['all_probes_passed']=all(c['state']=='evaluated' and c['evaluation']['probes_passed'] for c in report['cases'])
    report['candidate_parser_ms']=sum(c.get('document',{}).get('timings',{}).get('parser_total_ms',0) for c in report['cases'])
    save(path,report)


def execute(report,path,timeout=720,offline=False):
    return execute_cases(report,path,timeout,offline,validate,score_case,finish)


def execute_cases(report,path,timeout,offline,validate_report,evaluate_case,finalize_report):
    """Shared immutable request lifecycle; a strategy owns preparation/scoring."""
    validate_report(report,path)  # Every cache/seed before any upload or refinement.
    if offline:
        finalize_report(report,path); return
    api,model=API(report['api']),API(report['model_api'])
    for case in report['cases']:
        if case['state']!='pending':continue
        ready(model,report['identity'])
        seed=case['seed']
        # Persist a stable key/body before upload/refinement; a lost reply uses
        # exactly these values next time, never another source or model.
        raw=checked_source(seed['upload']); code,source=upload(api,seed['upload'],raw)
        expect(code,200,source);check_document(seed['upload'],source,raw)
        if case.get('source') and case['source']!=source:
            raise ValueError('Resumed upload source identity changed')
        case['source']=source;save(path,report)
        request=dict(seed['request'],document_id=case['source']['document_id'])
        if 'accepted' not in case:
            ready(model,report['identity'])
            code,accepted=api.call('/api/refinement-runs',request,seed['key']);expect(code,202,accepted)
            case['accepted']=accepted;save(path,report)
        deadline=time.monotonic()+timeout
        while True:
            run=get(api,'/api/refinement-runs/'+case['accepted']['refinement_run_id'])
            if (run.get('refinement_run_id')!=case['accepted']['refinement_run_id']
                    or run.get('operation_id')!=case['accepted']['operation_id']
                    or run.get('source_document_id')!=source['document_id'] or run.get('name')!=request['name']):
                raise ValueError('Recorded request/run identity mismatch')
            case['run']=run;save(path,report)
            if run.get('status') in ('failed','cancelled'):
                case['state']='failed';case['error']={k:run.get(k) for k in ('status','error_code','error_message')};break
            if run.get('submission_status')=='blocked':raise ValueError('Submission blocked; inspect the recorded run before resume')
            if run.get('status')=='succeeded':break
            if time.monotonic()>=deadline:raise ValueError('Request still pending; resume the printed report')
            time.sleep(2)
        if case['state']=='failed':
            finalize_report(report,path);print('[region]',case['id'],'JOB_FAILED',flush=True);continue
        case['metadata']=get(api,'/api/canonical-documents/'+run['output_document_id'])
        raw=download(api,'/api/canonical-documents/'+run['output_document_id']+'/content')
        doc=verified_case(report,case,raw)
        ready(model,report['identity'])
        report.setdefault('processor_image_id',doc['processor']['image_id'])
        target=path.parent/(case['id']+'.json');temp=target.with_suffix('.tmp');temp.write_bytes(raw);temp.replace(target)
        case.update(document=doc,evaluation=evaluate_case(case['id'],doc,report['baseline']),state='evaluated')
        finalize_report(report,path);print('[region]',case['id'],json.dumps(case['evaluation'],ensure_ascii=False),flush=True)
    finalize_report(report,path)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--api',default='http://127.0.0.1:14000');p.add_argument('--model-api',default='http://127.0.0.1:13000')
    p.add_argument('--source-id');p.add_argument('--deployment-id');p.add_argument('--baseline-run-id')
    p.add_argument('--output-root',type=Path,default=Path(__file__).with_name('results')/'document-regions')
    group=p.add_mutually_exclusive_group();group.add_argument('--resume',type=Path);group.add_argument('--evaluate',type=Path)
    p.add_argument('--timeout',type=int,default=720);args=p.parse_args()
    if args.timeout<1:p.error('--timeout must be positive')
    if args.resume or args.evaluate:
        if args.source_id or args.deployment_id or args.baseline_run_id:p.error('Resume uses recorded identifiers')
        path=(args.resume or args.evaluate).resolve();report=json.loads(path.read_bytes())
    else:
        if not args.source_id or not args.deployment_id:p.error('--source-id and --deployment-id are required')
        report,path=initialize(API(args.api),API(args.model_api),args.source_id,args.deployment_id,args.output_root,args.baseline_run_id)
    print('[record]',path,flush=True)
    with (path.parent/'experiment.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:execute(report,path,args.timeout,bool(args.evaluate))
        except Exception:
            print('[resume] Use --resume with the SAME report; no replacement experiment was submitted.',flush=True)
            raise
    print('[summary]',json.dumps({k:report[k] for k in ('complete','all_probes_passed','candidate_parser_ms')}),flush=True)
    print('Region experiment only. No production result was replaced; whole-document acceptance is separate.')
    return 0 if report['complete'] and all(c['state']=='evaluated' for c in report['cases']) else 1

if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,ValueError,KeyError,TypeError) as exc:raise SystemExit('ERROR: '+str(exc))
