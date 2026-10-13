"""Read-only, source-bound quality audit. Exit 1 means quality failure; 2 invalid input.

This does not submit jobs or change documents. --replay uses stored OCR responses,
verifies each rendered input hash and blocks HTTP calls; it is not GPU inference.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import runpy
import sys
import unicodedata
from unittest.mock import patch

PLATFORM = Path(__file__).resolve().parents[1]
REFERENCE = Path(__file__).with_name('fixtures') / 'document-quality/ai-engineer-sample.json'
MAX_BYTES = 32 * 1024 * 1024


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalized(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFC', text))


def read_bytes(path):
    with path.open('rb') as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('Input exceeds 32 MiB: ' + path.name)
    return data


def coverage(inner, outer):
    if not inner or not outer:
        return 0.0
    area = (inner[2] - inner[0]) * (inner[3] - inner[1])
    inter = max(0, min(inner[2], outer[2]) - max(inner[0], outer[0])) * max(
        0, min(inner[3], outer[3]) - max(inner[1], outer[1]))
    return inter / area if area > 0 else 0.0


def in_region(block, region):
    loc = block.get('location', {})
    return loc.get('page') == region['page'] and coverage(loc.get('bbox'), region['bbox']) >= 0.8


def grid(block):
    result = {}
    cells = block.get('cells', [])
    if not isinstance(cells, list) or len(cells) > 20000:
        raise ValueError('Invalid table size')
    for cell in cells:
        r, c, rs, cs = (cell.get('row'), cell.get('column'), cell.get('row_span', 1), cell.get('column_span', 1))
        if (any(type(x) is not int for x in (r, c, rs, cs)) or min(r, c) < 0 or min(rs, cs) < 1
                or max(r + rs, c + cs) > 1000 or len(result) + rs * cs > 20000):
            raise ValueError('Invalid table cell span')
        for row in range(r, r + rs):
            for col in range(c, c + cs):
                if (row, col) in result:
                    raise ValueError('Overlapping table cells')
                result[row, col] = normalized(cell['text'])
    return result


def cell_keys(cells):
    return sorted((c['row'], c['column'], c.get('row_span', 1), c.get('column_span', 1),
                   normalized(c['text'])) for c in cells)


def checks_for(value, reference):
    blocks = value['blocks']
    checks = []
    def record(name, passed, **details):
        checks.append({'check': name, 'passed': bool(passed), **details})
    order_ok = all(b.get('order') == i and b.get('id') == 'b' + str(i + 1) for i, b in enumerate(blocks))
    record('block_order_numbers', order_ok, note='Number consistency only, not semantic reading-order accuracy.')
    unresolved = [x for x in value.get('quality', {}).get('issues', []) if x.get('requires_ocr')]
    record('no_unresolved_ocr', value.get('quality', {}).get('state') != 'needs_ocr' and not unresolved,
           unresolved_issue_count=len(unresolved))
    figure = reference['figure']
    figure_text = '\n'.join(b.get('text', '') for b in blocks if in_region(b, figure))
    found = []
    for label in figure['labels']:
        pattern = r'(?<!\w)' + r'\s*'.join(re.escape(x) for x in label.split()) + r'(?!\w)'
        if re.search(pattern, unicodedata.normalize('NFC', figure_text)):
            found.append(label)
    missing = [x for x in figure['labels'] if x not in found]
    record('diagram_internal_labels', not missing, matched=len(found), expected=len(figure['labels']), missing=missing,
           note='Final blocks inside figure only; raw HTML and candidates cannot satisfy this check. Arrow relations are not scored.')
    region = reference['table1']
    candidates = [b for b in blocks if b['type'] == 'table' and in_region(b, region)]
    grids = [grid(b) for b in candidates]
    def matching_row(words, columns):
        wanted = [normalized(x) for x in words]
        return any(all(g.get((r, c)) == word for c, word in zip(columns, wanted))
                   for g in grids for r in {p[0] for p in g})
    for name, refs, columns in [('table1_process_labels', region['processes'], region['process_columns']),
                                 ('table1_item_amount_unit', region['triplets'], region['value_columns'])]:
        missed = [words for words in refs if not matching_row(words, columns)]
        record(name, not missed, matched=len(refs) - len(missed), expected=len(refs), missing=missed,
               note='Authored probes in combined 10-column Table 1; not total table accuracy.')
    region = reference['table2']
    candidates = [b for b in blocks if b['type'] == 'table' and in_region(b, region)]
    expected = cell_keys(region['cells'])
    actual = cell_keys(candidates[0]['cells']) if len(candidates) == 1 else []
    matched = sum(x in actual for x in expected)
    record('table2_all_cells_and_spans', len(candidates) == 1 and actual == expected,
           matched=matched, expected=len(expected), candidate_count=len(candidates),
           note='25 data rows + header; row/column, spans and text, NFC/whitespace only.')
    duplicates = []
    for table in (b for b in blocks if b['type'] == 'table'):
        texts = [normalized(c['text']) for c in table.get('cells', []) if normalized(c['text'])]
        if len(texts) < 2 or sum(map(len, texts)) < 100:
            continue
        for body in blocks:
            if body['type'] not in ('text', 'paragraph') or not body.get('text'):
                continue
            if (table.get('location', {}).get('page') == body.get('location', {}).get('page')
                    and coverage(table.get('location', {}).get('bbox'), body.get('location', {}).get('bbox')) >= 0.8
                    and all(t in normalized(body['text']) for t in texts)):
                duplicates.append({'table': table.get('id'), 'body': body.get('id'), 'page': body['location']['page']})
    record('no_overlapping_table_body_duplicate', not duplicates, duplicates=duplicates,
           note='Detects this overlapping duplicate pattern; not every possible duplication.')
    visual_duplicates = []
    for i, a in enumerate(blocks):
        for b in blocks[i+1:]:
            if not (a.get('visual_region_id') or b.get('visual_region_id')):
                continue
            if a.get('type') != 'paragraph' or b.get('type') != 'paragraph' or not normalized(a.get('text', '')):
                continue
            al, bl = a.get('location', {}), b.get('location', {})
            if (al.get('page') == bl.get('page') and al.get('coordinate_system') == bl.get('coordinate_system')
                    and normalized(a['text']) == normalized(b.get('text', ''))
                    and max(coverage(al.get('bbox'), bl.get('bbox')), coverage(bl.get('bbox'), al.get('bbox'))) >= 0.8):
                visual_duplicates.append({'first': a.get('id'), 'second': b.get('id'), 'page': al.get('page')})
    record('no_overlapping_visual_text_duplicate', not visual_duplicates, duplicates=visual_duplicates,
           note='Same text with overlapping visual/page output; repeated text elsewhere is preserved.')
    return checks


def stable(value):
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items() if k not in ('timings', 'total_ms')}
    if isinstance(value, list):
        return [stable(v) for v in value]
    return value


def replay(source, value):
    parser = load_module('sample_parser', PLATFORM / 'jobs/refinement/parse/parser.py')
    ocr = load_module('sample_ocr', PLATFORM / 'jobs/refinement/parse/ocr.py')
    records = value['ocr']['pages']
    answers = [('page-' + str(p['page']), p) for p in records]
    answers += [(r['id'], r) for p in records for r in p.get('visual_regions', [])]
    # Refuse incomplete evidence rather than call a live endpoint.
    for name, answer in answers:
        if not answer.get('raw_html') or not answer.get('input_png_sha256'):
            raise ValueError('Cannot replay missing response/image hash: ' + name)
    calls = []
    keys = ('raw_html', 'page_mean_token_probability', 'usage', 'finish_reason', 'input_png_sha256')
    def infer(image, pinned, deadline):
        if len(calls) >= len(answers):
            raise ValueError('Unexpected OCR request during replay')
        name, answer = answers[len(calls)]
        _, digest = ocr.payload_for(image, pinned)
        calls.append({'request': name, 'input_matches': digest == answer['input_png_sha256']})
        if not calls[-1]['input_matches']:
            raise ValueError('Rendered OCR input differs: ' + name)
        return {k: copy.deepcopy(answer[k]) for k in keys if k in answer}
    original = runpy.run_path
    def loader(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if Path(path).name == 'ocr.py':
            result['enrich'] = lambda doc, data, pinned, measure_timings=False: ocr.enrich(
                doc, data, pinned, infer_page=infer, measure_timings=measure_timings)
        return result
    spec = {'document_id': value['source']['document_id'], 'sha256': value['source']['sha256'], 'format': 'pdf',
            'image_id': value['processor']['image_id'], 'rules_version': value['processor']['rules_version'],
            'ocr': value['ocr']['identity'], 'measure_timings': False}
    with patch('runpy.run_path', side_effect=loader), patch('http.client.HTTPConnection', side_effect=RuntimeError('Network forbidden')):
        result = json.loads(parser.parse(source, spec))
    complete = len(calls) == len(answers) and all(x['input_matches'] for x in calls)
    return {'check': 'recorded_response_replay', 'passed': complete and stable(result) == stable(value),
            'requests': calls, 'expected_responses': len(answers), 'network_calls': 0,
            'output_equal_excluding_timings': stable(result) == stable(value),
            'note': 'Reproduces stored failures; PASS does not mean OCR quality passed. Not new inference.'}


def evaluate(source, value, markdown, reference, with_replay=False):
    sha = hashlib.sha256(source).hexdigest()
    if sha != reference['source_sha256'] or value.get('source', {}).get('sha256') != sha:
        raise ValueError('Source/reference/canonical hashes do not match')
    if value.get('schema_version') != 'canonical-document-v1' or len(value.get('pages', [])) != reference['page_count']:
        raise ValueError('Unexpected canonical schema or page count')
    checks = checks_for(value, reference)
    output = load_module('quality_data_results', PLATFORM / 'console/data_results.py')
    checks.append({'check': 'markdown_matches_final_json', 'passed': output.markdown_document(value) == markdown})
    if with_replay:
        checks.append(replay(source, value))
    return {'reference': reference['version'], 'source_sha256': sha, 'passed': all(x['passed'] for x in checks),
            'scope': reference['scope'], 'quality_state': value.get('quality', {}).get('state'), 'checks': checks}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', type=Path, required=True)
    ap.add_argument('--canonical', type=Path, required=True)
    ap.add_argument('--markdown', type=Path, required=True)
    ap.add_argument('--replay', action='store_true')
    args = ap.parse_args()
    try:
        result = evaluate(read_bytes(args.source), json.loads(read_bytes(args.canonical)),
                          read_bytes(args.markdown).decode('utf-8'), json.loads(REFERENCE.read_text()), args.replay)
    except (OSError, ValueError, KeyError, TypeError, ImportError, RuntimeError) as exc:
        print('INVALID INPUT: ' + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    sys.exit(main())
