"""Source-pixel region experiment for one authored sample; no inference or repair.

The plan contains positions only. Expected text never enters the generated PNG.
Contact-sheet reading order is not the original diagram's arrow/semantic order.
"""
import copy
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import re
import unicodedata

from audit_document_quality import REFERENCE, grid, normalized, checks_for

PLATFORM = Path(__file__).resolve().parents[1]
PLAN = Path(__file__).with_name('fixtures') / 'document-quality/regions.json'


def read_json(path):
    return json.loads(path.read_bytes())


def plan_and_reference():
    plan, ref = read_json(PLAN), read_json(REFERENCE)
    if plan.get('version') != 'ai-engineer-regions-v1' or plan['source_sha256'] != ref['source_sha256']:
        raise ValueError('Unsupported region plan/source')
    return plan, ref


def render_inputs(source, baseline, destination):
    # Source binding is checked before decoding any image or writing a crop.
    plan, ref = plan_and_reference()
    if hashlib.sha256(source).hexdigest() != plan['source_sha256'] or baseline['source']['sha256'] != plan['source_sha256']:
        raise ValueError('Source/reference/baseline hashes differ')
    if len(baseline['pages']) != ref['page_count']:
        raise ValueError('Wrong baseline page count')
    from PIL import Image
    spec = importlib.util.spec_from_file_location('region_ocr', PLATFORM/'jobs/refinement/parse/ocr.py')
    ocr = importlib.util.module_from_spec(spec); spec.loader.exec_module(ocr)
    outputs = []
    destination.mkdir(parents=True, exist_ok=True)
    for case in plan['cases']:
        page = baseline['pages'][case['page']-1]
        image = ocr.render_page(source, 'pdf', page)
        parts, mapping = [], []
        try:
            for box in case['boxes']:
                if (len(box) != 4 or any(type(x) not in (int, float) or not math.isfinite(x) for x in box)
                        or not 0 <= box[0] < box[2] <= page['width'] or not 0 <= box[1] < box[3] <= page['height']):
                    raise ValueError('Invalid source region')
                region = {'page': case['page'], 'bbox': box, 'coordinate_system': page['coordinate_system']}
                crop, location = ocr.render_visual(image, page, region)
                # Bound each diagram patch before laying it out; preserve aspect.
                if case['layout'] == 'contact-sheet': crop.thumbnail((700, 100), Image.Resampling.LANCZOS)
                parts.append(crop); mapping.append({'source_location': location})
            if case['layout'] == 'crop' and len(parts) == 1:
                result = parts[0].copy(); mapping[0]['image_bbox'] = [0, 0, result.width, result.height]
            elif case['layout'] == 'contact-sheet' and len(parts) == 24:
                result = Image.new('RGB', (1440, 12*140+20), 'white')
                for i,(part, location) in enumerate(zip(parts, mapping)):
                    x, y = 20+(i%2)*710, 20+(i//2)*140
                    result.paste(part,(x,y)); location['image_bbox'] = [x,y,x+part.width,y+part.height]
            else:
                raise ValueError('Unsupported region layout')
            try:
                stream = io.BytesIO(); result.save(stream,format='PNG'); data = stream.getvalue()
                target = destination/(case['id']+'.png')
                if target.exists(): raise ValueError('Do not overwrite previously prepared inputs')
                target.write_bytes(data)
                outputs.append({'id':case['id'],'file':target.name,'sha256':hashlib.sha256(data).hexdigest(),
                                'size_bytes':len(data),'width':result.width,'height':result.height,
                                'mapping':mapping,'layout':case['layout'],'page':case['page']})
            finally: result.close()
        finally:
            image.close()
            for part in parts: part.close()
    return outputs


def exact_label(label, text):
    # Do not let Washing II satisfy Washing I, or normalize changed punctuation.
    pattern = r'(?<!\w)' + r'\s*'.join(re.escape(x) for x in label.split()) + r'(?!\w)'
    return bool(re.search(pattern, unicodedata.normalize('NFC', text)))


def score_case(case_id, document, baseline):
    _, ref = plan_and_reference()
    baseline_checks = {x['check']: x for x in checks_for(baseline, ref)}
    blocks = document['blocks']
    if len(document['pages']) != 1:
        raise ValueError('Region input must have one image page')
    result = {'quality_state':document['quality']['state'],
              'scope':'Authored-region transcription probes, not full-document quality or automatic region detection.',
              'timings':document.get('timings',{}),
              'ocr_request_count':sum(bool(p.get('input_png_sha256'))+sum(bool(r.get('input_png_sha256')) for r in p.get('visual_regions',[])) for p in document.get('ocr',{}).get('pages',[]))}
    if case_id == 'figure-labels':
        text = '\n'.join(b.get('text','') for b in blocks)
        missing = [x for x in ref['figure']['labels'] if not exact_label(x,text)]
        result.update(matched=24-len(missing),expected=24,missing=missing,
                      baseline_matched=baseline_checks['diagram_internal_labels']['matched'],
                      probes_passed=not missing,
                      limitation='Label recall only; no arrow relations, semantic reading order or automatic source-box attribution.')
    elif case_id in ('table-left','table-right'):
        tables = [b for b in blocks if b['type']=='table']
        grids = []
        for table in tables:
            try:
                g = grid(table)
                if {c for _,c in g} == set(range(5)): grids.append(g)
            except ValueError: pass  # Invalid structure is not allowed to satisfy a probe.
        def score(probes, columns):
            missing = [words for words in probes if not any(all(g.get((r,c))==normalized(w) for c,w in zip(columns,words)) for g in grids for r in {p[0] for p in g})]
            return {'matched':len(probes)-len(missing),'expected':len(probes),'missing':missing}
        if case_id=='table-right':
            values=score(ref['table1']['triplets'],[2,3,4]); processes=score(ref['table1']['processes'],[0,1])
            result.update(values=values,processes=processes,probes_passed=not values['missing'] and not processes['missing'],
                          baseline_values_matched=baseline_checks['table1_item_amount_unit']['matched'],
                          baseline_processes_matched=baseline_checks['table1_process_labels']['matched'])
        else:
            # Independent left-half spot checks. Not a full table accuracy score.
            values=score([['Raw cowhide','1500','Kg'],['Electricity','8.52','kWh'],['Water, ground','2415','Kg']],[2,3,4])
            result.update(values=values,probes_passed=not values['missing'])
        result['table_count']=len(tables)
        result['limitation']='Same row and five-column position required; malformed spans fail. Probes do not cover every cell or the joined ten-column table.'
    else: raise ValueError('Unknown region case')
    return result
