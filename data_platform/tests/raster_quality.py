"""Independent process/section/item/amount/unit audit, no text repair or inference."""
from collections import Counter
import json
from pathlib import Path
from audit_document_quality import grid, normalized, REFERENCE
from document_regions import exact_label

REFERENCE_PATH=Path(__file__).with_name('fixtures')/'document-quality/raster-reference.json'
HEADER=tuple(normalized(s) for s in ('No.','Process','Inputs and Outputs','Amount','Unit'))


def reference():return json.loads(REFERENCE_PATH.read_bytes())


def table_records(document):
    records=[];errors=[];pairs=set()
    tables=[b for b in document['blocks'] if b['type']=='table']
    if len(tables)!=1:errors.append('expected_one_table')
    for table in tables:
        try:g=grid(table)
        except ValueError as e:errors.append(str(e));continue
        if {c for _,c in g}!=set(range(5)):errors.append('expected_five_columns')
        if tuple(g.get((0,c)) for c in range(5))!=HEADER:errors.append('header_mismatch')
        previous=None;section=''
        for r in sorted({r for r,c in g if r>0}):
            if any((r,c) not in g for c in range(5)):errors.append('missing_cell_at_row_'+str(r))
            pair=tuple(g.get((r,c),'') for c in (0,1))
            if pair!=previous:section='';previous=pair
            if any(pair):pairs.add(pair)
            item,amount,unit=(g.get((r,c),'') for c in (2,3,4))
            if item in (normalized('Inputs:'),normalized('Outputs:')) and not amount and not unit:
                section=item;continue
            if not any((item,amount,unit)):continue
            records.append((*pair,section,item,amount,unit))
    return records,sorted(set(errors)),pairs


def score_tables(document, expectations):
    records,errors,pairs=table_records(document)
    expected=[tuple(normalized(s) for s in (*case['process'],*row)) for case in expectations for row in case['rows']]
    got,want=Counter(records),Counter(expected)
    missing=list((want-got).elements());extra=list((got-want).elements())
    allowed={tuple(normalized(s) for s in c['process']) for c in expectations if any(c['process'])}
    unexpected=sorted(pairs-allowed)
    return {'matched':len(expected)-len(missing),'expected':len(expected),'missing':missing,'extra':extra,
            'structural_errors':errors,'unexpected_processes':unexpected,
            'passed':not errors and not missing and not extra and not unexpected,
            'scope':'Exact process number/name + Inputs/Outputs + item/amount/unit with multiplicity; no inferred process for unassigned leading rows.'}


def score_figure(document, mapping=None):
    labels=json.loads(REFERENCE.read_bytes())['figure']['labels']
    text='\n'.join(b.get('text','') for b in document['blocks'])
    missing=[x for x in labels if not exact_label(x,text)]
    result={'matched':len(labels)-len(missing),'expected':len(labels),'missing':missing,'passed':not missing,
            'scope':'Visible labels only, not arrow relations or semantic order.'}
    if mapping is not None:
        counts=Counter();mapped=[];unmapped=[];mismatched=[]
        for b in document['blocks']:
            if not b.get('text','').strip():continue
            box=b.get('location',{}).get('bbox',[]);matches=[]
            if len(box)==4 and b.get('location',{}).get('page')==1:
                area=(box[2]-box[0])*(box[3]-box[1])
                for i,m in enumerate(mapping):
                    a=m['image_bbox'];inter=max(0,min(a[2],box[2])-max(a[0],box[0]))*max(0,min(a[3],box[3])-max(a[1],box[1]))
                    if area>0 and inter/area>=.9:matches.append(i)
            if len(matches)!=1:unmapped.append(b['id']);continue
            i=matches[0];counts[i]+=1;m=mapping[i];a=m['image_bbox'];s=m['source_bbox']
            if i>=len(labels) or normalized(b['text'])!=normalized(labels[i]):mismatched.append(b['id'])
            restored=[s[j]+(box[j]-a[j])*(s[j+2]-s[j])/(a[j+2]-a[j]) for j in (0,1)]
            restored += [s[j]+(box[j+2]-a[j])*(s[j+2]-s[j])/(a[j+2]-a[j]) for j in (0,1)]
            mapped.append({'block_id':b['id'],'region':i,'source_bbox':restored,'source_page':m['source_page'],'coordinate_system':'rendered-page-pixels-top-left'})
        result.update(mapped_blocks=mapped,unmapped_blocks=unmapped,region_label_mismatches=mismatched,one_block_per_region=all(counts[i]==1 for i in range(len(mapping))))
        result['passed'] &= not unmapped and not mismatched and result['one_block_per_region'] and len(mapping)==len(labels)
    return result


def score_input(item,document):
    if len(document.get('pages',[]))!=1:raise ValueError('Expected one PNG page')
    if item['id']=='figure-labels':quality=score_figure(document,item['mapping'])
    else:
        case=next(c for c in reference()['cases'] if c['id']==item['id'])
        quality=score_tables(document,[case])
    return {'probes_passed':quality['passed'],'quality_state':document['quality']['state'],
            'quality':quality,'timings':document.get('timings',{}),
            'scope':'Raster candidate experiment; original document not replaced.'}


def audit_previous(report):
    if report.get('source',{}).get('sha256')!=reference()['source_sha256'] or not report.get('complete'):
        raise ValueError('Need a completed report for the authored source')
    if [c['id'] for c in report['cases']]!=['figure-labels','table-left','table-right'] or any(c.get('state')!='evaluated' for c in report['cases']):
        raise ValueError('Incomplete or unknown previous cases')
    result={}
    for c in report['cases']:
        d=c['document']
        if d.get('ocr',{}).get('identity')!=report['identity'] or d['source']['sha256']!=c['seed']['input']['sha256']:
            raise ValueError('Recorded runtime/source identity differs')
        if c['id']=='figure-labels':result[c['id']]=score_figure(d)
        else:result[c['id']]=score_tables(d,[x for x in reference()['cases'] if x['id'].startswith(c['id'])])
    return {'all_passed':all(x['passed'] for x in result.values()),'cases':result,
            'scope':'Offline content re-evaluation of supplied inline JSON; original raw-download bytes are not included, so their hashes are not reverified. No GPU/API request.'}

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report',type=Path,required=True);a=p.parse_args()
    result=audit_previous(json.loads(a.report.read_bytes()));print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['all_passed'] else 1)
