#!/usr/bin/env python3
"""Pixel-only region candidates through existing upload/refinement APIs. No original rewrite."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import uuid

import live_document_regions as shared
from live_collection_smoke import API,save
from live_upload_smoke import make_report as upload_report,checked_source,check_document
from live_refinement_smoke import verify_result
from raster_document import prepare
from raster_quality import REFERENCE_PATH,reference,score_input

VERSION='raster-regions-v1'


def code_identity():
    names=('live_raster_regions.py','raster_document.py','raster_regions.py','raster_quality.py')
    files=[Path(__file__).with_name(n) for n in names]+[REFERENCE_PATH]
    return shared.digest({'shared':shared.code_identity(),'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in files}})


def initialize(api,model,source_id,deployment_id,root,baseline_id=None):
    source_id,deployment_id=str(uuid.UUID(source_id)),str(uuid.UUID(deployment_id))
    source=shared.get(api,'/api/source-documents/'+source_id)
    data=shared.download(api,'/api/source-documents/'+source_id+'/content')
    if source.get('state')!='ready' or source.get('sha256')!=hashlib.sha256(data).hexdigest() or source.get('size_bytes')!=len(data) or source['sha256']!=reference()['source_sha256']:
        raise ValueError('Source differs; no work submitted')
    run=shared.baseline_run(api,source_id,deployment_id,baseline_id)
    meta=shared.get(api,'/api/canonical-documents/'+run['output_document_id'])
    raw=shared.download(api,'/api/canonical-documents/'+meta['document_id']+'/content')
    baseline=verify_result(source,run,meta,raw);shared.ready(model,baseline['ocr']['identity'])
    key=str(uuid.uuid4());path=root.resolve()/key/'report.json'
    inputs=prepare(data,path.parent/'inputs')  # Does not receive baseline/parsed PDF metadata.
    (path.parent/'baseline.json').write_bytes(raw)
    uploads=upload_report(api,[path.parent/'inputs'/i['file'] for i in inputs])['uploads'];cases=[]
    for item,up in zip(inputs,uploads):
        name='raster-experiment-'+key+'-'+item['id'];up['name']=name
        seed={'input':item,'upload':up,'key':str(uuid.uuid4()),'request':{'name':name,'rules_version':'basic-cleaning-v1','ocr_deployment_id':deployment_id}}
        cases.append({'id':item['id'],'seed':seed,'seed_sha256':shared.digest(seed),'state':'pending'})
    report={'version':VERSION,'key':key,'code_identity':code_identity(),'api':api.base,'model_api':model.base,
            'source':source,'baseline_run':run,'baseline_metadata':meta,'baseline':baseline,'identity':baseline['ocr']['identity'],
            'cases':cases,'complete':False,'all_probes_passed':False,'production_result_modified':False,
            'scope':'Automatic pixel geometry on authored sample pages 3/4. Nine candidate PNGs; no PDF text/parsed boxes, no arrow semantics or production merge.'}
    report['fixed_sha256']=shared.fixed_digest(report);save(path,report);return report,path


def evaluate(report,case,document):return score_input(case['seed']['input'],document)


def validate(report,path):
    if report.get('version')!=VERSION or report.get('code_identity')!=code_identity() or report.get('fixed_sha256')!=shared.fixed_digest(report):
        raise ValueError('Code/baseline/report identity changed; no requests submitted')
    if report['source']['sha256']!=reference()['source_sha256']:raise ValueError('Wrong source')
    expected=['figure-labels']+[c['id'] for c in reference()['cases']]
    if [c['id'] for c in report['cases']]!=expected:raise ValueError('Case set changed')
    if verify_result(report['source'],report['baseline_run'],report['baseline_metadata'],(path.parent/'baseline.json').read_bytes())!=report['baseline']:
        raise ValueError('Baseline cache changed')
    if report['identity']!=report['baseline']['ocr']['identity']:raise ValueError('Baseline runtime changed')
    for c in report['cases']:
        seed=c['seed'];i=seed['input']
        if shared.digest(seed)!=c['seed_sha256'] or i['id']!=c['id']:raise ValueError('Input/request mapping changed')
        if Path(seed['upload']['path']).resolve()!=(path.parent/'inputs'/i['file']).resolve():raise ValueError('Input path changed')
        data=checked_source(seed['upload'])
        if hashlib.sha256(data).hexdigest()!=i['sha256'] or len(data)!=i['size_bytes']:raise ValueError('Input pixels changed')
        if c.get('source'):check_document(seed['upload'],c['source'],data)
        if c['state'] not in ('pending','failed','evaluated'):raise ValueError('Unknown case state')
        if c['state']=='evaluated':
            doc=shared.verified_case(report,c,(path.parent/(c['id']+'.json')).read_bytes())
            if doc!=c.get('document'):raise ValueError('Canonical inline/cache mismatch')
            c['evaluation']=evaluate(report,c,doc)


def execute(report,path,timeout=720,offline=False):
    cases={c['id']:c for c in report['cases']}
    def scorer(id,doc,baseline):return evaluate(report,cases[id],doc)
    return shared.execute_cases(report,path,timeout,offline,validate,scorer,shared.finish)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--api',default='http://127.0.0.1:14000');p.add_argument('--model-api',default='http://127.0.0.1:13000')
    p.add_argument('--source-id');p.add_argument('--deployment-id');p.add_argument('--baseline-run-id')
    p.add_argument('--output-root',type=Path,default=Path(__file__).with_name('results')/'raster-regions')
    g=p.add_mutually_exclusive_group();g.add_argument('--resume',type=Path);g.add_argument('--evaluate',type=Path)
    p.add_argument('--timeout',type=int,default=720);a=p.parse_args()
    if a.timeout<1:p.error('Positive timeout required')
    if a.resume or a.evaluate:
        if a.source_id or a.deployment_id or a.baseline_run_id:p.error('Resume uses saved identifiers')
        path=(a.resume or a.evaluate).resolve();report=json.loads(path.read_bytes())
    else:
        if not a.source_id or not a.deployment_id:p.error('Source and deployment required')
        report,path=initialize(API(a.api),API(a.model_api),a.source_id,a.deployment_id,a.output_root,a.baseline_run_id)
    print('[record]',path,flush=True)
    with (path.parent/'experiment.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:execute(report,path,a.timeout,bool(a.evaluate))
        except Exception:
            print('[resume] Use the SAME report; do not submit a replacement experiment.',flush=True);raise
    print('[summary]',json.dumps({k:report[k] for k in ('complete','all_probes_passed','candidate_parser_ms')}),flush=True)
    print('Candidate experiment only; complete does not mean correct. Original document unchanged.')
    return 0 if report['complete'] and all(c['state']=='evaluated' for c in report['cases']) else 1

if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,ValueError,KeyError,TypeError) as e:raise SystemExit('ERROR: '+str(e))
