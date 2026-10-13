"""Deterministic quality measurements for canonical JSON (standard library only)."""
import hashlib
import html
import json
import math
from pathlib import Path
import re
import unicodedata

RUBRIC = 'ocr-quality-v1'
GEOMETRY_RUBRIC = 'ocr-geometry-v1'
SUPPORTED_RUBRICS = (RUBRIC, GEOMETRY_RUBRIC)
MAX_CER = 0.02
MAX_TEXT = 5000


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFC', text) if not c.isspace())


def edit_distance(left, right):
    if len(left) > len(right): left, right = right, left
    previous = list(range(len(left) + 1))
    for row, b in enumerate(right, 1):
        current = [row]
        for col, a in enumerate(left, 1):
            current.append(min(previous[col] + 1, current[-1] + 1, previous[col - 1] + (a != b)))
        previous = current
    return previous[-1]


def text_score(reference, actual):
    left, right = normalize(reference), normalize(actual)
    if not left or max(len(left), len(right)) > MAX_TEXT:
        return {'passed': False, 'reason': 'empty_reference_or_text_limit', 'cer': None,
                'reference_characters': len(left), 'actual_characters': len(right)}
    edits = edit_distance(left, right)
    return {'passed': edits / len(left) <= MAX_CER, 'cer': edits / len(left), 'edits': edits,
            'reference_characters': len(left), 'actual_characters': len(right), 'max_cer': MAX_CER}


def occurrence(text, token):
    positions = [m.start() for m in re.finditer(re.escape(normalize(token)), text)]
    return positions[0] if len(positions) == 1 else None


def order_score(markers, actual):
    text = normalize(actual)
    positions = [occurrence(text, token) for token in markers]
    present = all(p is not None for p in positions)
    return {'passed': present and positions == sorted(positions), 'markers': markers,
            'positions': positions, 'missing_or_duplicate': [m for m, p in zip(markers, positions) if p is None]}


def valid_box(box, width, height):
    return (isinstance(box, list) and len(box) == 4 and all(type(n) in (int, float) and math.isfinite(n) for n in box)
            and 0 <= box[0] < box[2] <= width + 0.1 and 0 <= box[1] < box[3] <= height + 0.1)


def geometry_score(anchors, blocks, width, height):
    all_valid = all(valid_box(b.get('location', {}).get('bbox'), width, height) for b in blocks)
    offset, spans, text = 0, [], ''
    for block in blocks:
        value = normalize(block.get('text', ''))
        spans.append((offset, offset + len(value), block.get('location', {}).get('bbox')))
        offset += len(value); text += value
    measured = []
    for anchor in anchors:
        start = occurrence(text, anchor['text'])
        boxes = []
        if start is not None:
            end = start + len(normalize(anchor['text']))
            boxes = [box for a, b, box in spans if a < end and b > start and valid_box(box, width, height)]
        box = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)] if boxes else None
        region = anchor['bbox']; inside = False
        if box:
            x, y = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            inside = region[0] - 8 <= x <= region[2] + 8 and region[1] - 8 <= y <= region[3] + 8
        measured.append({'text': anchor['text'], 'passed': inside, 'expected_region': region, 'actual_bbox': box})
    return {'passed': all_valid and bool(blocks) and all(a['passed'] for a in measured),
            'all_boxes_in_page': all_valid, 'anchors': measured,
            'criterion': 'unique anchor; block-union center inside reference region plus 8 source coordinate units'}


def table_score(expected, actual):
    results = []
    for index, table in enumerate(expected):
        found = actual[index] if index < len(actual) else {'cells': []}
        cells = found.get('cells', [])
        mapped, duplicate = {}, False
        for c in cells:
            key = (c.get('row'), c.get('column'))
            duplicate |= key in mapped
            mapped[key] = c
        positions = {(c['row'], c['column']) for c in table['cells']}
        values_ok = spans_ok = 0
        differences = []
        for c in table['cells']:
            key = (c['row'], c['column']); candidate = mapped.get(key)
            value_ok = candidate is not None and normalize(candidate.get('text', '')) == normalize(c['text'])
            span_ok = candidate is not None and all(candidate.get(k, 1) == c[k] for k in ('row_span', 'column_span'))
            values_ok += value_ok; spans_ok += span_ok
            if not value_ok or not span_ok:
                differences.append({'expected': c, 'actual': candidate})
        count = len(table['cells'])
        results.append({'passed': not duplicate and set(mapped) == positions and values_ok == spans_ok == count,
                        'expected_cells': count, 'actual_cells': len(cells), 'cell_value_accuracy': values_ok / count,
                        'span_accuracy': spans_ok / count, 'duplicate_positions': duplicate,
                        'extra_positions': [list(k) for k in sorted(set(mapped) - positions)], 'differences': differences})
    return {'passed': len(expected) == len(actual) and all(v['passed'] for v in results),
            'expected_tables': len(expected), 'actual_tables': len(actual), 'tables': results}


def evaluate_guarded_case(case, document, identity):
    """Pass only a preserved coordinate rejection; never label it OCR success."""
    ocr = document.get('ocr') or {}
    attempts = ocr.get('pages') or []
    issues = document.get('quality', {}).get('issues', [])
    checks = {'provenance': ocr.get('identity') == identity and ocr.get('client_version') == 'surya-openai-v1',
              'processor': document.get('processor', {}).get('version') == 'cpu-parser-surya-v1',
              'selective_policy': ocr.get('policy') == 'selective-pages-v1',
              'quality_state': document.get('quality', {}).get('state') == 'needs_ocr',
              'page_selection': [a.get('page') for a in attempts] == [p['number'] for p in case['pages']],
              'page_count': len(document.get('pages', [])) == len(case['pages'])}
    pages = []
    for expected in case['pages']:
        number = expected['number']
        attempt = next((p for p in attempts if p.get('page') == number), {})
        actual_page = next((p for p in document.get('pages', []) if p.get('number') == number), {})
        blocks = [b for b in document.get('blocks', []) if b.get('location', {}).get('page') == number]
        preserved = attempt.get('native_blocks') == blocks and bool(blocks)
        page_checks = {
            'expected_rejection': attempt.get('status') == 'failed' and attempt.get('reason') == expected['expected_reason'],
            'native_preserved': preserved and all(b.get('extraction') != 'surya-openai-v1' for b in blocks),
            'native_issues_preserved': bool(attempt.get('native_issues')) and all(i in issues for i in attempt.get('native_issues', [])),
            'unresolved_issue_retained': any(i.get('code') == 'ocr_' + expected['expected_reason'] and i.get('requires_ocr') is True
                                            and i.get('location', {}).get('page') == number for i in issues),
            'no_inference_record': not any(k in attempt for k in ('rendered_image', 'raw_html', 'usage', 'input_png_sha256', 'finish_reason', 'page_mean_token_probability')),
            'dimensions_match': all(abs(actual_page.get(k, 0) - expected[k]) < 1 for k in ('width', 'height')),
            'coordinates_match': actual_page.get('coordinate_system') == expected['coordinate_system']
                                 and actual_page.get('rotation') == expected['rotation'],
        }
        page = {'number': number, 'mode': 'guarded', 'passed': all(page_checks.values()), 'guard_checks': page_checks,
                'reference_text': expected['text'], 'actual_text': '\n'.join(b.get('text', '') for b in blocks)}
        for metric in ('text', 'reading_order', 'geometry', 'table'):
            page[metric] = {'passed': None, 'cer': None, 'not_applicable': True, 'reason': 'expected coordinate rejection; OCR not performed'}
        pages.append(page)
    return {'case_id': case['id'], 'outcome': 'coordinate_guard', 'ocr_succeeded': False,
            'passed': all(checks.values()) and all(p['passed'] for p in pages), 'checks': checks,
            'pages': pages, 'ocr_pages': [{'page': p.get('page'), 'status': p.get('status'), 'reason': p.get('reason')} for p in attempts],
            'quality_issues': issues}


def evaluate_case(case, document, identity):
    if all(p['mode'] == 'guarded' for p in case['pages']):
        return evaluate_guarded_case(case, document, identity)
    ocr = document.get('ocr') or {}
    wanted = [p['number'] for p in case['pages'] if p['mode'] == 'ocr']
    attempts = ocr.get('pages') or []
    selection = [p.get('page') for p in attempts] == wanted
    checks = {'provenance': ocr.get('identity') == identity and ocr.get('client_version') == 'surya-openai-v1',
              'selective_policy': ocr.get('policy') == 'selective-pages-v1',
              'processor': document.get('processor', {}).get('version') == 'cpu-parser-surya-v1',
              'page_selection': selection,
              'ocr_completed': bool(attempts) and all(p.get('status') == 'completed' for p in attempts),
              'quality_state': document.get('quality', {}).get('state') == 'needs_review',
              'review_retained': any(i.get('code') == 'ocr_output_requires_review' for i in document.get('quality', {}).get('issues', [])),
              'no_unresolved_ocr': not any(i.get('requires_ocr') for i in document.get('quality', {}).get('issues', [])),
              'page_count': len(document.get('pages', [])) == len(case['pages'])}
    pages = []
    for expected in case['pages']:
        number = expected['number']
        actual_page = next((p for p in document.get('pages', []) if p.get('number') == number), {})
        blocks = [b for b in document.get('blocks', []) if b.get('location', {}).get('page') == number]
        prose = '\n'.join(b.get('text', '') for b in blocks if b.get('type') != 'table')
        measured = {'number': number, 'mode': expected['mode'],
                    'text': text_score(expected['text'], prose),
                    'reading_order': order_score(expected['reading_order'], prose),
                    'geometry': geometry_score(expected['anchors'], blocks, expected['width'], expected['height']),
                    'table': table_score(expected['tables'], [b for b in blocks if b.get('type') == 'table']),
                    'reference_text': expected['text'], 'actual_text': prose}
        dimensions = all(abs(actual_page.get(k, 0) - expected[k]) < 1 for k in ('width', 'height'))
        native = expected['mode'] != 'native' or all(b.get('extraction') != 'surya-openai-v1' for b in blocks)
        coordinates = ('coordinate_system' not in expected or
                       (actual_page.get('coordinate_system') == expected['coordinate_system'] and
                        all(b.get('location', {}).get('coordinate_system') == expected['coordinate_system'] for b in blocks)))
        rotation = 'rotation' not in expected or actual_page.get('rotation') == expected['rotation']
        measured['passed'] = dimensions and native and coordinates and rotation and all(measured[k]['passed'] for k in ('text', 'reading_order', 'geometry', 'table'))
        measured['dimensions_match'], measured['native_preserved'] = dimensions, native
        measured['coordinates_match'], measured['rotation_match'] = coordinates, rotation
        pages.append(measured)
    return {'case_id': case['id'], 'passed': all(checks.values()) and all(p['passed'] for p in pages),
            'checks': checks, 'pages': pages, 'ocr_pages': [{'page': p.get('page'), 'status': p.get('status'), 'reason': p.get('reason')} for p in attempts],
            'quality_issues': document.get('quality', {}).get('issues', [])}


def load_suite(path):
    raw = path.read_bytes()
    if len(raw) > 1 << 20: raise ValueError('Suite exceeds 1 MiB')
    suite = json.loads(raw)
    if suite.get('schema_version') != 1 or suite.get('rubric_version') not in SUPPORTED_RUBRICS or not 1 <= len(suite.get('cases', [])) <= 20:
        raise ValueError('Unsupported quality suite')
    geometry = suite['rubric_version'] == GEOMETRY_RUBRIC
    seen = set()
    for case in suite['cases']:
        if not re.fullmatch('[a-z0-9-]{1,60}', case['id']) or case['id'] in seen:
            raise ValueError('Invalid or duplicate case ID')
        seen.add(case['id'])
        suffix = Path(case['file']).suffix
        if Path(case['file']).name != case['file'] or suffix not in (('.pdf', '.jpg', '.jpeg') if geometry else ('.pdf',)):
            raise ValueError('Fixture must be an allowed file in the suite directory')
        source = path.parent / case['file']
        if source.stat().st_size > 32 << 20 or hashlib.sha256(source.read_bytes()).hexdigest() != case['sha256']:
            raise ValueError('Fixture bytes changed: ' + case['id'])
        if not 1 <= len(case['pages']) <= 10: raise ValueError('Fixture page count out of range')
        for n, p in enumerate(case['pages'], 1):
            if p['number'] != n or p['mode'] not in (('ocr', 'native', 'guarded') if geometry else ('ocr', 'native')) or not 1 <= len(normalize(p['text'])) <= MAX_TEXT:
                raise ValueError('Invalid reference page')
            if geometry:
                if p.get('coordinate_system') != ('pdf-points-top-left' if suffix == '.pdf' else 'image-pixels-top-left'):
                    raise ValueError('Invalid geometry coordinate system')
                if suffix == '.pdf' and p.get('rotation') not in (0, 90, 180, 270):
                    raise ValueError('Invalid expected PDF rotation')
                if p['mode'] == 'guarded' and (suffix != '.pdf' or p.get('expected_reason') != 'pdf_coordinate_mismatch'):
                    raise ValueError('Only an explicit PDF coordinate guard is supported')
            if not all(type(p[k]) in (int, float) and math.isfinite(p[k]) and 0 < p[k] <= 5000 for k in ('width', 'height')):
                raise ValueError('Invalid reference page dimensions')
            if not p['anchors'] or not p['reading_order']: raise ValueError('Missing reference anchors')
            for anchor in p['anchors']:
                if not normalize(anchor['text']) or not valid_box(anchor['bbox'], p['width'], p['height']):
                    raise ValueError('Invalid reference anchor')
            if not order_score(p['reading_order'], p['text'])['passed']:
                raise ValueError('Reference reading order is ambiguous')
            for table in p['tables']:
                if not valid_box(table['bbox'], p['width'], p['height']) or not 1 <= len(table['cells']) <= 200:
                    raise ValueError('Invalid reference table')
                occupied = set()
                for cell in table['cells']:
                    if (any(type(cell[k]) is not int or not 0 <= cell[k] < 100 for k in ('row', 'column'))
                            or any(type(cell[k]) is not int or not 1 <= cell[k] <= 100 for k in ('row_span', 'column_span'))):
                        raise ValueError('Invalid reference table position/span')
                    positions = {(r, c) for r in range(cell['row'], cell['row'] + cell['row_span'])
                                 for c in range(cell['column'], cell['column'] + cell['column_span'])}
                    if occupied & positions: raise ValueError('Overlapping reference table cells')
                    occupied |= positions
                    if not normalize(cell['text']): raise ValueError('Empty reference table cell')
        if any(p['mode'] == 'guarded' for p in case['pages']):
            if any(p['mode'] != 'guarded' for p in case['pages']): raise ValueError('Guard cases must be separate from OCR quality cases')
        elif not any(p['mode'] == 'ocr' for p in case['pages']): raise ValueError('Case requires an OCR page')
    return suite, hashlib.sha256(raw).hexdigest()


def write_html(report, path):
    def escaped(value): return html.escape(str(value), quote=True)
    parts = ['<!doctype html><meta charset="utf-8"><title>OCR 품질·좌표 검증</title>',
             '<style>body{font:15px/1.65 sans-serif;max-width:1050px;margin:40px auto;padding:0 24px;color:#172230}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd5de;padding:8px;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f6f9;padding:16px}h2{margin-top:36px}.pass{color:#14683b}.fail{color:#a11e32}</style>',
             '<h1>OCR 품질·좌표 검증</h1>', '<p>검사 기준 충족: <strong>' + ('PASS' if report.get('passed') else '미통과 또는 진행 중') + '</strong></p>',
             '<p>OCR 품질 사례: 공백을 제외한 NFC 문자 오류율 ≤ 2%. 읽기 순서·중요 문구·표 값과 병합·페이지 선택도 각각 통과해야 합니다.</p>',
             '<p>좌표 불일치 보존 사례의 GUARD_PASS는 OCR 성공이 아닙니다. needs_ocr와 native 결과를 보존했는지 확인하며 문자·표 점수는 N/A입니다.</p>']
    for item in report.get('cases', []):
        result = item.get('evaluation')
        parts.append('<h2>' + escaped(item['id']) + '</h2>')
        if not result:
            parts.append('<pre>' + escaped(item.get('error', '아직 평가하지 않음')) + '</pre>'); continue
        label = 'GUARD_PASS (OCR 미수행)' if result.get('outcome') == 'coordinate_guard' else 'PASS'
        parts.append('<p class="' + ('pass' if result['passed'] else 'fail') + '">' + (label if result['passed'] else 'FAIL') + '</p>')
        for page in result['pages']:
            parts.append('<h3>페이지 ' + str(page['number']) + '</h3><table><tr><th>항목</th><th>결과</th></tr>')
            for key in ('text', 'reading_order', 'geometry', 'table'):
                value = page[key]
                brief = ('PASS' if value['passed'] else 'FAIL')
                if value.get('not_applicable'): brief = 'N/A - 좌표 불일치 보존 검사'
                elif key == 'text': brief += ' / CER: ' + str(value.get('cer'))
                parts.append('<tr><td>' + escaped(key) + '</td><td>' + escaped(brief) + '</td></tr>')
            parts.append('</table><details><summary>정답과 인식 결과</summary><h4>정답</h4><pre>' + escaped(page['reference_text']) + '</pre><h4>인식 결과</h4><pre>' + escaped(page['actual_text']) + '</pre></details>')
        parts.append('<details><summary>판정·좌표·표·품질 이슈 상세</summary><pre>' + escaped(json.dumps(result, ensure_ascii=False, indent=2)) + '</pre></details>')
    parts.append('<p>합성 문서의 기능·품질 회귀 검사입니다. 실제 운영 문서 전체의 정확도를 보장하지 않습니다. 원시 모델 HTML은 실행하지 않고 데이터로만 보존합니다.</p>')
    path.write_text('\n'.join(parts), encoding='utf-8')
