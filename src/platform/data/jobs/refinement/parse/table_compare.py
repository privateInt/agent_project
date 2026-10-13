"""OCR-first PDF table selection with preserved comparison candidates. No inference calls, text repair or voting.

Candidates are immutable snapshots. Disagreement is evidence for review, not
proof that either extractor is correct. Model probability is never a selector.
"""
import copy
import math
import re
import unicodedata

VERSION = 'table-comparison-v1'
POLICY = 'ocr-priority-v1'
MAX_GRID = 20000
MAX_TABLES = 500


def normalized(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFC', text))


def numbers(text):
    # Preserve signs, punctuation, leading zeros and the position of each value.
    return re.findall(r'[+−-]?\d+(?:[.,:/%−-]\d+)*%?', normalized(text))


def box(block):
    value = block.get('location', {}).get('bbox')
    if (not isinstance(value, list) or len(value) != 4 or
            any(type(v) not in (int, float) or not math.isfinite(v) for v in value) or
            value[0] >= value[2] or value[1] >= value[3]):
        return None
    return value


def overlap(a, b):
    if a is None or b is None:
        return 0.0, 0.0
    area_a, area_b = (a[2]-a[0])*(a[3]-a[1]), (b[2]-b[0])*(b[3]-b[1])
    inter = max(0, min(a[2], b[2])-max(a[0], b[0])) * max(0, min(a[3], b[3])-max(a[1], b[1]))
    return inter / (area_a+area_b-inter), inter / min(area_a, area_b)


def snapshot(block):
    # Never include model HTML in the comparison renderer or selection logic.
    fields = ('type', 'text', 'cells', 'location', 'extraction', 'visual_region_id', 'model_bbox', 'model_coordinate_system')
    result = copy.deepcopy({k: block[k] for k in fields if k in block})
    return result


def assess(block):
    cells = block.get('cells')
    problems, occupied, anchors = set(), set(), {}
    rows = columns = nonempty = 0
    if not isinstance(cells, list) or not cells or len(cells) > MAX_GRID:
        return {'valid': False, 'rows': 0, 'columns': 0, 'nonempty_cells': 0,
                'issues': ['cell_limit' if cells else 'empty_table']}, {}
    for cell in cells:
        if not isinstance(cell, dict) or not isinstance(cell.get('text'), str):
            problems.add('invalid_cell')
            continue
        r, c, rs, cs = (cell.get('row'), cell.get('column'), cell.get('row_span', 1), cell.get('column_span', 1))
        if (any(type(v) is not int for v in (r, c, rs, cs)) or min(r, c) < 0 or min(rs, cs) < 1 or
                max(r+rs, c+cs) > 1000 or rs*cs > MAX_GRID or len(occupied)+rs*cs > MAX_GRID):
            problems.add('invalid_span')
            continue
        area = {(rr, cc) for rr in range(r, r+rs) for cc in range(c, c+cs)}
        if area & occupied:
            problems.add('overlapping_cells')
        occupied.update(area)
        rows, columns = max(rows, r+rs), max(columns, c+cs)
        text = normalized(cell['text'])
        nonempty += bool(text)
        if '\ufffd' in text or '(cid:' in text or any(unicodedata.category(ch) == 'Cc' for ch in text):
            problems.add('unreadable_text')
        anchors[r, c] = (rs, cs, text)
    if rows*columns > MAX_GRID:
        problems.add('grid_limit')
    elif len(occupied) != rows*columns:
        problems.add('missing_cells')
    if not nonempty:
        problems.add('empty_text')
    return {'valid': not problems, 'rows': rows, 'columns': columns, 'nonempty_cells': nonempty,
            'issues': sorted(problems)}, anchors


def decide(native, recognized, confident, unavailable):
    nm = [assess(b) for b in native]
    om = [assess(b) for b in recognized]
    nvalid = bool(nm) and all(x[0]['valid'] for x in nm)
    ovalid = bool(om) and all(x[0]['valid'] for x in om)
    inferred = any(b.get('extraction') == 'pdf-native-text-grid-v1' for b in native)
    selected = 'ocr' if inferred and recognized else 'parser' if nvalid or not recognized else 'ocr'
    status, reasons, differences = 'needs_review', [], []
    if not native or not recognized:
        reasons.append(unavailable if not recognized and unavailable else 'ocr_missing' if not recognized else 'parser_missing')
    elif len(native) != 1 or len(recognized) != 1 or not confident:
        reasons.append('ambiguous_regions')
    else:
        na, oa = nm[0][1], om[0][1]
        numeric = structural = textual = False
        for position in sorted(na.keys() | oa.keys()):
            a, b = na.get(position), oa.get(position)
            kind = 'missing_cell' if a is None or b is None else 'span' if a[:2] != b[:2] else 'text'
            if a == b:
                continue
            structural |= a is None or b is None or a[:2] != b[:2]
            textual |= a is not None and b is not None and a[2] != b[2]
            numeric |= a is not None and b is not None and bool(a[2]) and bool(b[2]) and numbers(a[2]) != numbers(b[2])
            differences.append({'row': position[0], 'column': position[1], 'kind': kind,
                                'parser': a[2] if a else None, 'ocr': b[2] if b else None})
        if numeric: reasons.append('numeric_conflict')
        if structural: reasons.append('structure_conflict')
        if textual: reasons.append('text_conflict')
        if nvalid and ovalid and not differences:
            status, reasons, selected = 'agreed', ['agreement'], 'parser'
        elif nvalid != ovalid:
            # A malformed/empty candidate can be discarded only if existing
            # readable content is preserved. Conflicting numbers always review.
            bad, good = (om, nm) if nvalid else (nm, om)
            bad_text = [v[2] for v in bad[0][1].values() if v[2]]
            good_text = [v[2] for v in good[0][1].values() if v[2]]
            content_preserved = not bad_text or sorted(bad_text) == sorted(good_text)
            if not numeric and content_preserved and not inferred:
                status, reasons = 'selected', ['ocr_invalid' if nvalid else 'parser_invalid']
        if not nvalid: reasons.append('parser_invalid')
        if not ovalid: reasons.append('ocr_invalid')
    if inferred: reasons.append('parser_grid_inferred')
    # Preference is explicit: preserve usable OCR even when structures/values
    # conflict. Disagreement remains visible and is never an accuracy claim.
    usable_ocr = bool(om) and all(m[0]['nonempty_cells'] > 0 for m in om)
    preferred = 'ocr' if usable_ocr or not native else 'parser'
    if status == 'selected' and preferred != selected:
        status = 'needs_review'
    selected = preferred
    if usable_ocr:
        reasons.append('ocr_priority')
    elif recognized and native:
        reasons.append('ocr_empty_fallback')
    return {'status': status, 'selected': selected, 'reasons': sorted(set(reasons)),
            'parser_metrics': [x[0] for x in nm], 'ocr_metrics': [x[0] for x in om],
            'difference_count': len(differences), 'differences': differences[:100],
            'differences_truncated': len(differences) > 100}


def groups(native, recognized):
    """Connected overlaps avoid greedy matching of split/merged adjacent tables."""
    nodes = [('parser', i) for i in range(len(native))] + [('ocr', i) for i in range(len(recognized))]
    graph = {node: set() for node in nodes}
    for i, a in enumerate(native):
        for j, b in enumerate(recognized):
            iou, coverage = overlap(box(a), box(b))
            if iou >= 0.1 or coverage >= 0.25:
                graph['parser', i].add(('ocr', j))
                graph['ocr', j].add(('parser', i))
    seen = set()
    for node in nodes:
        if node in seen: continue
        pending, component = [node], set()
        while pending:
            current = pending.pop()
            if current in component: continue
            component.add(current)
            pending.extend(graph[current] - component)
        seen.update(component)
        yield ([native[i] for side, i in sorted(component) if side == 'parser'],
               [recognized[i] for side, i in sorted(component) if side == 'ocr'])


def prose_already_preserved(table, existing, recognized):
    """Suppress only a sparse false table whose ordered prose is already in OCR.

    Keep valid tables, headers, OCR table matches and any content/geometry doubt.
    The original candidate remains in ocr.pages[].native_blocks as evidence.
    """
    tb = box(table)
    if tb is None or any(overlap(tb, box(b))[1] >= 0.25 for b in recognized):
        return False
    metrics, _ = assess(table)
    if metrics['issues'] != ['missing_cells']:
        return False
    cells = [c for c in table['cells'] if normalized(c['text'])]
    if (len(cells) < 2 or len({c['row'] for c in cells}) != len(cells)
            or any(c.get('header') or c.get('row_span', 1) != 1
                   or len(normalized(c['text'])) < 30 or len(c['text'].split()) < 4 for c in cells)):
        return False
    text = ''.join(normalized(c['text']) for c in sorted(cells, key=lambda c: (c['row'], c['column'])))
    for block in existing:
        bb = box(block)
        if (block['type'] not in ('paragraph', 'text') or block.get('visual_region_id') or bb is None
                or block['location'].get('page') != table['location'].get('page')):
            continue
        if (bb[0] <= tb[0] and bb[1] <= tb[1] and bb[2] >= tb[2] and bb[3] >= tb[3]
                and text in normalized(block.get('text', ''))):
            return True
    return False


def reconcile(doc, region_extractor=None):
    if doc.value['source']['format'] != 'pdf':
        return
    records, merged = [], []
    region_attempts = 0
    current = doc.value['blocks']
    pages = {r['page']: r for r in doc.value.get('ocr', {}).get('pages', [])}
    for page in doc.value['pages']:
        number = page['number']
        existing = [b for b in current if b.get('location', {}).get('page') == number]
        ocr_page = pages.get(number)
        completed = ocr_page is not None and ocr_page['status'] in ('completed', 'partial')
        native = [b for b in (ocr_page['native_blocks'] if ocr_page else existing) if b['type'] == 'table']
        recognized = [b for b in existing if b['type'] == 'table'] if completed else []
        if region_extractor is not None:
            for recognized_table in recognized:
                # Only supplement tables the first native pass did not locate.
                if any(overlap(box(n), box(recognized_table))[1] >= 0.25 for n in native): continue
                if region_attempts >= 50: break
                region_attempts += 1
                native.extend(region_extractor(page, recognized_table))
        if not native and not recognized:
            merged.extend(existing)
            continue
        if len(native)+len(recognized) > MAX_TABLES:
            raise ValueError('table comparison limit exceeded')
        if completed:
            kept = [b for b in native if not prose_already_preserved(b, existing, recognized)]
            if len(kept) != len(native):
                doc.issue('pdf_native_table_text_already_in_ocr_body', {'page': number})
            native = kept
            if not native and not recognized:
                merged.extend(existing)
                continue
        output = [b for b in existing if b['type'] != 'table']
        # Retain OCR reading order. Replacements occupy their OCR table's slot;
        # a missing table is placed before the next geometric content block.
        insertions = []
        unavailable = 'ocr_not_requested' if 'ocr' not in doc.value else 'ocr_unavailable' if not completed else ''
        for a, b in groups(native, recognized):
            confident = len(a) == len(b) == 1 and overlap(box(a[0]), box(b[0]))[0] >= 0.5
            decision = decide(a, b, confident, unavailable)
            identity = 't' + str(len(records)+1)
            boxes = [box(x) for x in a+b if box(x) is not None]
            bbox = [min(v[0] for v in boxes), min(v[1] for v in boxes), max(v[2] for v in boxes), max(v[3] for v in boxes)] if boxes else None
            record = {'id': identity, 'page': number, 'bbox': bbox,
                      'coordinate_system': page['coordinate_system'],
                      'parser_candidates': [snapshot(x) for x in a], 'ocr_candidates': [snapshot(x) for x in b],
                      **decision, 'result_block_ids': []}
            records.append(record)
            selected = a if decision['selected'] == 'parser' else b
            chosen = [snapshot(x) for x in selected]
            for block in chosen:
                block['table_comparison_id'] = identity
            anchors = b if completed else a
            position = min((existing.index(x) for x in anchors), default=-1)
            if position < 0:
                position = next((i for i, x in enumerate(existing) if box(x) and bbox and box(x)[1] >= bbox[1]), len(existing))
            if decision['selected'] == 'ocr':
                insertions.extend((existing.index(original), [replacement]) for original, replacement in zip(b, chosen))
            else:
                insertions.append((position, chosen))
            if completed and decision['selected'] == 'parser':
                # OCR sometimes emits native table contents as paragraphs.
                # Remove only wholly contained text, never neighboring text.
                def duplicated(x):
                    if x.get('visual_region_id'): return False
                    xb = box(x)
                    if x['type'] not in ('text', 'paragraph', 'heading') or xb is None: return False
                    for table in chosen:
                        tb = box(table)
                        if tb and tb[0] <= xb[0] and tb[1] <= xb[1] and tb[2] >= xb[2] and tb[3] >= xb[3]: return True
                    return False
                output = [x for x in output if not duplicated(x)]
            if decision['status'] == 'needs_review':
                doc.issue('table_comparison_requires_review', {'page': number, 'table_id': identity})
        scheduled = {}
        for position, chosen in insertions:
            scheduled.setdefault(position, []).extend(chosen)
        keep = {id(x) for x in output}
        for i, block in enumerate(existing):
            merged.extend(scheduled.get(i, []))
            if id(block) in keep: merged.append(block)
        merged.extend(scheduled.get(len(existing), []))
    if not records:
        return
    by_id = {r['id']: r for r in records}
    for i, block in enumerate(merged):
        block['id'], block['order'] = 'b'+str(i+1), i
        if block.get('table_comparison_id') in by_id:
            by_id[block['table_comparison_id']]['result_block_ids'].append(block['id'])
    doc.value['blocks'] = merged
    doc.value['table_comparison'] = {'version': VERSION, 'policy': POLICY, 'tables': records}
