"""Bounded CPU extraction. No remote resources, macros, formula evaluation or OCR.

The Go job owns execution authorization, source integrity and result registration.
This process receives only local paths and immutable provenance in a JSON spec.
"""
import argparse
from collections import Counter
from contextlib import closing, ExitStack
import hashlib
import io
from itertools import combinations
import json
import math
import posixpath
import re
from statistics import median
import sys
import time
import unicodedata
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

SCHEMA = 'canonical-document-v1'
PROCESSOR = 'cpu-parser-v1'
RULES = 'basic-cleaning-v1'
MAX_BYTES = 32 << 20
MAX_BLOCKS = 100000
MAX_CELLS = 100000
MAX_PAGES = 300


class ParseError(ValueError):
    pass


def local(tag):
    return tag.rsplit('}', 1)[-1]


def attr(node, key, default=''):
    return next((v for k, v in node.attrib.items() if local(k) == key), default)


def children(node, name):
    return [c for c in node if local(c.tag) == name]


def descendant(node, name):
    return next((c for c in node.iter() if local(c.tag) == name), None)


def integer(value, low=0, high=MAX_CELLS):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ParseError('invalid structure integer') from None
    if not low <= n <= high:
        raise ParseError('structure limit exceeded')
    return n


class Document:
    def __init__(self, spec):
        self.value = {'schema_version': SCHEMA, 'source': {'document_id': spec['document_id'],
            'sha256': spec['sha256'], 'format': spec['format']},
            'processor': {'version': PROCESSOR, 'rules_version': RULES, 'image_id': spec['image_id']},
            'blocks': [], 'pages': [], 'quality': {'state': 'ready', 'issues': []},
            'cleaning': {'version': RULES, 'rules': ['line-endings', 'unicode-nfc-prose'],
                         'changed_fields': 0, 'protected_fields': ['code', 'markdown', 'formula', 'cell_value']}}
        self.cells = 0

    def clean(self, text, protected=False):
        result = text.replace('\r\n', '\n').replace('\r', '\n')
        if not protected:
            result = unicodedata.normalize('NFC', result)
        if result != text:
            self.value['cleaning']['changed_fields'] += 1
        return result

    def add(self, kind, **fields):
        blocks = self.value['blocks']
        if len(blocks) >= MAX_BLOCKS:
            raise ParseError('block limit exceeded')
        block = {'id': 'b' + str(len(blocks) + 1), 'order': len(blocks), 'type': kind, **fields}
        self.cells += len(block.get('cells', []))
        if self.cells > MAX_CELLS:
            raise ParseError('cell limit exceeded')
        blocks.append(block)
        return block

    def issue(self, code, location=None, ocr=False):
        quality = self.value['quality']
        item = {'code': code, 'requires_ocr': ocr}
        if location is not None:
            item['location'] = location
        if item not in quality['issues']:
            quality['issues'].append(item)
        if ocr:
            quality['state'] = 'needs_ocr'
        elif quality['state'] == 'ready':
            quality['state'] = 'needs_review'


def package(data):
    parts = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if len(z.infolist()) > 4096:
            raise ParseError('ZIP entry limit exceeded')
        for entry in z.infolist():
            name = entry.filename
            if (name in parts or name.startswith('/') or '\\' in name or
                    posixpath.normpath(name.rstrip('/')) != name.rstrip('/') or
                    name.startswith('../') or ':' in name or entry.flag_bits & 1):
                raise ParseError('unsafe ZIP part')
            total += entry.file_size
            if total > 128 << 20 or entry.file_size > MAX_BYTES:
                raise ParseError('expanded ZIP limit exceeded')
            if entry.is_dir():
                continue
            with z.open(entry) as stream:
                raw = stream.read(entry.file_size + 1)
            if len(raw) != entry.file_size:
                raise ParseError('ZIP size mismatch')
            parts[name] = raw
    return parts


def xml(parts, name):
    data = parts.get(name)
    if data is None:
        raise ParseError('required OOXML part is missing')
    # Expat supports UTF-16; decode first so a DTD cannot hide behind NUL bytes.
    try:
        from lxml import etree
        root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True, huge_tree=False))
        if root.getroottree().docinfo.doctype:
            raise ParseError('DTD is unsupported')
        return ET.fromstring(etree.tostring(root))
    except ParseError:
        raise
    except Exception:
        raise ParseError('invalid or excessively nested OOXML') from None


def word_text(node):
    text = []
    for item in node.iter():
        kind = local(item.tag)
        if kind in ('t', 'instrText'):
            text.append(item.text or '')
        elif kind == 'tab':
            text.append('\t')
        elif kind in ('br', 'cr'):
            text.append('\n')
    return ''.join(text)


def docx(doc, data):
    parts = package(data)
    root = xml(parts, 'word/document.xml')
    body = descendant(root, 'body')
    if body is None:
        raise ParseError('DOCX body missing')
    for index, node in enumerate(body):
        location = {'part': 'word/document.xml', 'body_index': index}
        kind = local(node.tag)
        if kind == 'p':
            text = doc.clean(word_text(node), protected=True)
            style = descendant(node, 'pStyle')
            doc.add('paragraph', text=text, style=attr(style, 'val') if style is not None else '', location=location)
        elif kind == 'tbl':
            cells, active = [], {}
            for row_index, row in enumerate(children(node, 'tr')):
                before = descendant(row.find('{*}trPr'), 'gridBefore') if row.find('{*}trPr') is not None else None
                col = integer(attr(before, 'val', '0')) if before is not None else 0
                continued = set()
                for cell in children(row, 'tc'):
                    props = cell.find('{*}tcPr')
                    span = props.find('{*}gridSpan') if props is not None else None
                    width = integer(attr(span, 'val', '1'), 1, 16384) if span is not None else 1
                    merge = props.find('{*}vMerge') if props is not None else None
                    text = '\n'.join(word_text(p) for p in children(cell, 'p'))
                    key = (col, width)
                    if merge is not None and attr(merge, 'val') != 'restart':
                        anchor = active.get(key)
                        if anchor is None:
                            raise ParseError('DOCX vertical merge has no matching anchor')
                        anchor['row_span'] += 1
                        continued.add(key)
                        if text:
                            doc.issue('merged_continuation_contains_text', location)
                            anchor.setdefault('continuation_text', []).append(doc.clean(text, True))
                    else:
                        item = {'row': row_index, 'column': col, 'row_span': 1, 'column_span': width, 'text': doc.clean(text, True)}
                        cells.append(item)
                        if merge is not None:
                            active[key] = item
                            continued.add(key)
                    if children(cell, 'tbl'):
                        # Retain nested content explicitly, while flagging lost nested grid geometry.
                        doc.issue('nested_table_geometry_requires_review', location)
                        target_cell = active[key] if merge is not None and attr(merge, 'val') != 'restart' else cells[-1]
                        target_cell['nested_text'] = [word_text(t) for t in children(cell, 'tbl')]
                    col += width
                    if len(cells) > MAX_CELLS:
                        raise ParseError('cell limit exceeded')
                active = {k: v for k, v in active.items() if k in continued}
            doc.add('table', cells=cells, location=location)
        elif kind != 'sectPr':
            doc.add('unhandled_structure', text=doc.clean(word_text(node), True), location=location)
            doc.issue('docx_body_structure_requires_review', location)
        if any(local(n.tag) in ('drawing', 'pict', 'object') for n in node.iter()):
            doc.issue('embedded_visual_requires_ocr_review', location, ocr=True)
        if any(local(n.tag) in ('oMath', 'oMathPara') for n in node.iter()):
            doc.issue('equation_structure_requires_review', location)
    if any(re.match(r'word/(header|footer|footnotes|endnotes|comments)', name) for name in parts):
        doc.issue('ancillary_docx_parts_not_extracted')
    doc.issue('docx_pagination_unavailable')


def relationship_target(base, target):
    if '\\' in target or ':' in target:
        raise ParseError('invalid internal relationship')
    result = posixpath.normpath(posixpath.join(base, target)) if not target.startswith('/') else target[1:]
    if result.startswith('../') or result == '..':
        raise ParseError('relationship escapes package')
    return result


def xlsx(doc, data):
    parts = package(data)
    book = xml(parts, 'xl/workbook.xml')
    rels = xml(parts, 'xl/_rels/workbook.xml.rels')
    relationships = {attr(r, 'Id'): r for r in rels}
    shared = []
    if 'xl/sharedStrings.xml' in parts:
        for item in xml(parts, 'xl/sharedStrings.xml'):
            shared.append(''.join(n.text or '' for n in item.iter() if local(n.tag) == 't'))
    formats = {}
    styles = []
    if 'xl/styles.xml' in parts:
        style_root = xml(parts, 'xl/styles.xml')
        for n in style_root.iter():
            if local(n.tag) == 'numFmt':
                formats[attr(n, 'numFmtId')] = attr(n, 'formatCode')
        group = descendant(style_root, 'cellXfs')
        if group is not None:
            styles = [dict(n.attrib) for n in group]
    props = descendant(book, 'workbookPr')
    for sheet_index, sheet in enumerate(n for n in book.iter() if local(n.tag) == 'sheet'):
        if sheet_index >= MAX_PAGES:
            raise ParseError('sheet limit exceeded')
        rel = relationships.get(attr(sheet, 'id'))
        if rel is None or attr(rel, 'TargetMode') == 'External' or not attr(rel, 'Type').endswith('/worksheet'):
            raise ParseError('worksheet relationship unavailable')
        target = relationship_target('xl', attr(rel, 'Target'))
        root = xml(parts, target)
        sheet_name = attr(sheet, 'name')
        cells, seen = [], set()
        for cell in (n for n in root.iter() if local(n.tag) == 'c'):
            address = attr(cell, 'r')
            match = re.fullmatch(r'([A-Z]{1,3})([1-9][0-9]{0,6})', address)
            if match is None or address in seen:
                raise ParseError('invalid or duplicate cell address')
            col = 0
            for char in match[1]:
                col = col * 26 + ord(char) - 64
            if col > 16384 or int(match[2]) > 1048576:
                raise ParseError('cell address out of range')
            seen.add(address)
            kind = attr(cell, 't', 'n')
            value_node = descendant(cell, 'v')
            value = value_node.text or '' if value_node is not None else ''
            if kind == 's':
                idx = integer(value)
                if idx >= len(shared):
                    raise ParseError('shared string index out of range')
                value = shared[idx]
            elif kind == 'inlineStr':
                value = ''.join(n.text or '' for n in cell.iter() if local(n.tag) == 't')
            item = {'address': address, 'row': int(match[2]) - 1, 'column': col - 1,
                    'value': value, 'value_type': kind, 'style_index': attr(cell, 's', '0')}
            formula = descendant(cell, 'f')
            if formula is not None:
                item['formula'] = formula.text or ''
                item['formula_attributes'] = dict(formula.attrib)
                item['cached_value'] = value
                if attr(formula, 't') in ('shared', 'array'):
                    doc.issue('shared_or_array_formula_not_expanded', {'sheet': sheet_name, 'cell': address})
            cells.append(item)
            if len(cells) + doc.cells > MAX_CELLS:
                raise ParseError('cell limit exceeded')
        merged = [attr(n, 'ref') for n in root.iter() if local(n.tag) == 'mergeCell']
        doc.add('table', cells=cells, merged_ranges=merged, location={'sheet': sheet_name, 'sheet_index': sheet_index},
                sheet_state=attr(sheet, 'state', 'visible'), date_1904=attr(props, 'date1904', '0') if props is not None else '0',
                number_formats=formats, cell_styles=styles)
        if any(local(n.tag) in ('drawing', 'legacyDrawing', 'oleObjects') for n in root.iter()):
            doc.issue('spreadsheet_visuals_not_extracted', {'sheet': sheet_name}, ocr=True)
    if any(name.startswith('xl/externalLinks/') for name in parts):
        doc.issue('external_workbook_links_not_followed')


def html(doc, data):
    from lxml import etree
    # A declared encoding is used when present; UTF-8 is the default for uploaded text.
    encoding = None if re.search(br'<meta[^>]+charset\s*=', data[:8192], re.I) else 'utf-8'
    html_parser = etree.HTMLParser(no_network=True, encoding=encoding, huge_tree=False, remove_comments=True)
    root = etree.fromstring(data, parser=html_parser)
    if any('depth' in entry.message.lower() or 'limit' in entry.message.lower() for entry in html_parser.error_log):
        raise ParseError('HTML structure limit exceeded')
    if root is None:
        raise ParseError('HTML document unavailable')
    for node in root.xpath('//script|//style|//noscript|//template'):
        # Drop dangerous/noncontent subtrees, preserving text after them.
        tail = node.tail
        parent = node.getparent()
        if parent is not None:
            previous = node.getprevious()
            parent.remove(node)
            if tail:
                if previous is None:
                    parent.text = (parent.text or '') + tail
                else:
                    previous.tail = (previous.tail or '') + tail
    def inline(node):
        if node.tag == 'br':
            return '\n'
        return (node.text or '') + ''.join(inline(c) + (c.tail or '') for c in node)
    def visit(node):
        tag = node.tag.lower() if isinstance(node.tag, str) else ''
        location = {'xpath': root.getroottree().getpath(node)}
        if tag == 'table':
            cells = []
            occupied = set()
            rows = [r for r in node.iter('tr') if next(r.iterancestors('table'), None) is node]
            for ri, row in enumerate(rows):
                col = 0
                for cell in row:
                    if cell.tag not in ('td', 'th'):
                        continue
                    while (ri, col) in occupied:
                        col += 1
                    rs = integer(cell.get('rowspan', '1'), 0, 1000)
                    if rs == 0:
                        rs = len(rows) - ri
                    cs = integer(cell.get('colspan', '1'), 1, 1000)
                    if rs * cs + len(occupied) > MAX_CELLS:
                        raise ParseError('HTML table grid limit exceeded')
                    for rr in range(ri, ri + rs):
                        for cc in range(col, col + cs):
                            if (rr, cc) in occupied:
                                raise ParseError('overlapping HTML cells')
                            occupied.add((rr, cc))
                    cells.append({'row': ri, 'column': col, 'row_span': rs, 'column_span': cs,
                                  'header': cell.tag == 'th', 'text': doc.clean(inline(cell), True)})
                    col += cs
            doc.add('table', cells=cells, location=location)
            if list(node.iterdescendants('table')):
                doc.issue('nested_html_table_geometry_requires_review', location)
            if list(node.iterdescendants('img')):
                doc.issue('html_table_image_not_fetched', location, ocr=True)
            return
        if tag == 'img':
            doc.add('image', alt=node.get('alt', ''), source_reference=node.get('src', ''), location=location)
            doc.issue('html_image_not_fetched', location, ocr=True)
            return
        if tag == 'pre':
            doc.add('code', text=doc.clean(inline(node), True), location=location)
            return
        if tag in ('p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6') and not node.xpath('.//img|.//table|.//pre'):
            doc.add('heading' if tag.startswith('h') else 'paragraph', text=doc.clean(inline(node), bool(node.xpath('.//code'))), location=location)
            return
        if node.text and node.text.strip():
            doc.add('text', text=doc.clean(node.text, tag == 'code'), location=location)
        for child in node:
            visit(child)
            if child.tail and child.tail.strip():
                doc.add('text', text=doc.clean(child.tail), location=location)
    bodies = root.xpath('//body')
    visit(bodies[0] if bodies else root)


def pdf_has_borderless_table(page):
    """Flag repeated numeric columns for OCR, without inventing a native grid.

    Four separate lines must share three widely spaced cell anchors, including
    a numeric cell. Ordinary paragraphs and two-column prose are not enough.
    This is a conservative routing hint, not a general table recognizer.
    """
    rows = []
    for word in sorted(page.extract_words(), key=lambda w: (w['top'], w['x0'])):
        if not rows or abs(word['top'] - rows[-1][0]['top']) > 3:
            rows.append([])
        rows[-1].append(word)
    signatures = Counter()
    for row in rows:
        row.sort(key=lambda w: w['x0'])
        gap = max(12, 1.5 * median(w['bottom'] - w['top'] for w in row))
        cells = []
        for word in row:
            if not cells or word['x0'] - cells[-1]['right'] > gap:
                cells.append({'left': word['x0'], 'right': word['x1'], 'text': word['text']})
            else:
                cells[-1]['text'] += ' ' + word['text']
                cells[-1]['right'] = max(cells[-1]['right'], word['x1'])
        # Bound combinations on unusual pages; keep all native text regardless.
        if not 3 <= len(cells) <= 20:
            continue
        anchors = []
        for cell in cells:
            numeric = bool(re.fullmatch(r'[+\-−]?\d[\d,.]*(?:[eE][+\-]?\d+)?%?', cell['text']))
            anchors.append((cell['right'] if numeric else cell['left'], numeric))
        # Try two staggered grids so small alignment jitter at a bin boundary
        # does not discard an otherwise repeated column layout.
        for offset in (0, 3):
            seen = set()
            for triple in combinations(anchors, 3):
                if not any(numeric for _, numeric in triple):
                    continue
                key = (offset, tuple((round((x + offset) / 6), numeric) for x, numeric in triple))
                seen.add(key)
            signatures.update(seen)
            if any(signatures[key] >= 4 for key in seen):
                return True
    return False


def pdf_table_cells(table, doc):
    # Infer spans from the physical cell edges, including merged rectangles.
    # pdfplumber's row/column placeholders alone lose these spans.
    entries = {}
    values = table.extract()
    for ri, row in enumerate(table.rows):
        for ci, bbox in enumerate(row.cells):
            if bbox is not None:
                value = values[ri][ci] if ri < len(values) and ci < len(values[ri]) else ''
                entries.setdefault(tuple(bbox), doc.clean(value or '', True))
    def edges(axis):
        result = []
        for value in sorted({b[i] for b in entries for i in (axis, axis+2)}):
            if not result or value-result[-1] > 0.5:
                result.append(value)
        return result
    xs, ys = edges(0), edges(1)
    result = []
    for bbox, text in sorted(entries.items(), key=lambda item: (item[0][1], item[0][0])):
        left, right = [min(range(len(xs)), key=lambda i: abs(xs[i]-bbox[k])) for k in (0, 2)]
        top, bottom = [min(range(len(ys)), key=lambda i: abs(ys[i]-bbox[k])) for k in (1, 3)]
        result.append({'row': top, 'column': left, 'row_span': bottom-top, 'column_span': right-left,
                       'bbox': list(bbox), 'text': text})
    return result


def pdf_native(doc, data, checkpoint):
    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as source:
        if len(source.pages) > MAX_PAGES:
            raise ParseError('PDF page limit exceeded')
        for index, page in enumerate(source.pages):
            number = index + 1
            loc = {'page': number, 'coordinate_system': 'pdf-points-top-left'}
            doc.value['pages'].append({'number': number, 'width': page.width, 'height': page.height,
                                       'rotation': page.rotation, 'coordinate_system': 'pdf-points-top-left'})
            tables = page.find_tables()
            if not tables and pdf_has_borderless_table(page):
                doc.issue('pdf_borderless_table_requires_ocr_review', loc, ocr=True)
            candidates = []
            boxes = []
            for table in tables:
                boxes.append(table.bbox)
                cells = pdf_table_cells(table, doc)
                candidates.append((table.bbox[1], table.bbox[0], 'table', {'cells': cells, 'location': {**loc, 'bbox': list(table.bbox)}}))
                doc.issue('pdf_table_spans_and_borderless_tables_require_review', loc)
            outside = page.filter(lambda obj: not any(
                box[0] <= (obj.get('x0', -1) + obj.get('x1', -1)) / 2 <= box[2] and
                box[1] <= (obj.get('top', -1) + obj.get('bottom', -1)) / 2 <= box[3] for box in boxes))
            for line in outside.extract_text_lines(layout=False, strip=False, return_chars=False):
                bbox = [line['x0'], line['top'], line['x1'], line['bottom']]
                candidates.append((bbox[1], bbox[0], 'text', {'text': doc.clean(line['text'], True), 'location': {**loc, 'bbox': bbox}}))
            for img in page.images:
                bbox = [img['x0'], img['top'], img['x1'], img['bottom']]
                candidates.append((bbox[1], bbox[0], 'image', {'location': {**loc, 'bbox': bbox}}))
                doc.issue('pdf_image_region_requires_ocr_review', {**loc, 'bbox': bbox}, ocr=True)
            for _, _, kind, fields in sorted(candidates, key=lambda x: (x[0], x[1])):
                doc.add(kind, **fields)
            text = ''.join(c.get('text', '') for c in page.chars)
            if not text.strip():
                doc.issue('pdf_page_has_no_extractable_text', loc, ocr=True)
            elif '\ufffd' in text or '(cid:' in text or sum(c.isalnum() for c in text) < 3:
                doc.issue('pdf_text_quality_requires_ocr_review', loc, ocr=True)
            doc.issue('pdf_reading_order_is_geometric_heuristic', loc)
            page.close()
            checkpoint.update(pages=len(doc.value['pages']), blocks=len(doc.value['blocks']),
                              issues=len(doc.value['quality']['issues']), cells=doc.cells,
                              changed_fields=doc.value['cleaning']['changed_fields'])


def pdf(doc, data):
    checkpoint = {'pages': 0, 'blocks': 0, 'issues': 0, 'cells': 0, 'changed_fields': 0}
    try:
        pdf_native(doc, data, checkpoint)
        return
    except (ParseError, MemoryError, TimeoutError, RecursionError, ImportError):
        # Limits/dependency failures must not be converted into extra work.
        raise
    except Exception:
        pass  # Native extraction failed; inspect page geometry independently.
    # Discard only the incomplete page. Finished native pages remain unchanged.
    for key in ('pages', 'blocks'):
        del doc.value[key][checkpoint[key]:]
    del doc.value['quality']['issues'][checkpoint['issues']:]
    doc.cells = checkpoint['cells']
    doc.value['cleaning']['changed_fields'] = checkpoint['changed_fields']
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(data) as source:
        count = len(source)
        if count < 1 or count > MAX_PAGES or count < checkpoint['pages']:
            raise ParseError('PDF page count unavailable or exceeds limit')
        for index in range(checkpoint['pages'], count):
            with closing(source[index]) as page:
                width, height = page.get_size()
                if not all(math.isfinite(v) and v > 0 for v in (width, height)):
                    raise ParseError('PDF page geometry invalid')
                doc.value['pages'].append({'number': index + 1, 'width': width, 'height': height,
                    'rotation': page.get_rotation(), 'coordinate_system': 'pdf-points-top-left'})
                doc.issue('pdf_native_parse_failed_requires_ocr', {'page': index + 1}, ocr=True)
    issues = doc.value['quality']['issues']
    doc.value['quality']['state'] = 'needs_ocr' if any(i['requires_ocr'] for i in issues) else ('needs_review' if issues else 'ready')


def parse(data, spec):
    started = time.monotonic()
    measure_timings = spec.get('measure_timings') is True
    if not data or len(data) > MAX_BYTES:
        raise ParseError('source size out of range')
    if hashlib.sha256(data).hexdigest() != spec['sha256']:
        raise ParseError('source checksum mismatch')
    if spec.get('rules_version') != RULES:
        raise ParseError('unsupported cleaning version')
    doc = Document(spec)
    kind = spec['format']
    doc.value['processing'] = {'version': 'refinement-processing-v1',
                               'mode': 'ocr_priority' if spec.get('ocr') is not None else 'parser_only'}
    if kind in ('text', 'markdown'):
        text = data.decode('utf-8-sig', errors='strict')
        doc.add('markdown' if kind == 'markdown' else 'text', text=doc.clean(text, True), location={'line_start': 1})
    elif kind == 'html':
        html(doc, data)
    elif kind == 'docx':
        docx(doc, data)
    elif kind == 'xlsx':
        xlsx(doc, data)
    elif kind == 'pdf':
        pdf(doc, data)
    elif kind in ('png', 'jpeg'):
        from PIL import Image
        with Image.open(io.BytesIO(data)) as img:
            if img.width * img.height > 100000000:
                raise ParseError('image pixel limit exceeded')
            img.verify()
            width, height = img.size
        doc.value['pages'].append({'number': 1, 'width': width, 'height': height, 'coordinate_system': 'image-pixels-top-left'})
        doc.add('image', location={'page': 1, 'bbox': [0, 0, width, height], 'coordinate_system': 'image-pixels-top-left'})
        doc.issue('image_requires_ocr', {'page': 1}, ocr=True)
    else:
        raise ParseError('unsupported source format')
    if not doc.value['blocks']:
        doc.issue('no_content_extracted')
    native_finished = time.monotonic()
    if spec.get("ocr") is not None:
        import runpy
        helper = runpy.run_path(str(Path(__file__).with_name("ocr.py")))
        helper["enrich"](doc, data, spec["ocr"], measure_timings=measure_timings)
    comparison_started = time.monotonic()
    if kind == 'pdf':
        import runpy
        helper = runpy.run_path(str(Path(__file__).with_name('table_compare.py')))
        with ExitStack() as resources:
            region_source = []
            def region_tables(page_meta, recognized):
                try:
                    if not region_source:
                        import pdfplumber
                        region_source.append(resources.enter_context(pdfplumber.open(io.BytesIO(data))))
                    page = region_source[0].pages[page_meta['number']-1]
                    bbox = recognized['location']['bbox']
                    cropped = page.crop(tuple(bbox))
                    tables = cropped.find_tables({'vertical_strategy': 'text', 'horizontal_strategy': 'text',
                                                  'min_words_vertical': 2, 'min_words_horizontal': 1})
                    result = []
                    for table in tables[:20]:
                        cells = pdf_table_cells(table, doc)
                        if not cells or len(cells) > 20000: continue
                        result.append({'type': 'table', 'cells': cells, 'extraction': 'pdf-native-text-grid-v1',
                            'location': {'page': page_meta['number'], 'bbox': list(table.bbox),
                                         'coordinate_system': 'pdf-points-top-left'}})
                    return result
                except (MemoryError, TimeoutError, RecursionError, ImportError):
                    raise
                except Exception:
                    # OCR still remains usable if the native text layer cannot
                    # be read. Do not alter or repair the original PDF.
                    return []
            helper['reconcile'](doc, region_extractor=region_tables)
    if measure_timings:
        finished = time.monotonic()
        doc.value['timings'] = {'version': 'refinement-timing-v1', 'clock': 'monotonic',
            'native_parse_ms': round((native_finished - started) * 1000, 3),
            'ocr_ms': round((comparison_started - native_finished) * 1000, 3) if spec.get('ocr') is not None else 0,
            'table_comparison_ms': round((finished - comparison_started) * 1000, 3),
            'parser_total_ms': round((finished - started) * 1000, 3)}
    raw = json.dumps(doc.value, ensure_ascii=False, allow_nan=False, separators=(',', ':'), sort_keys=True).encode('utf-8')
    if len(raw) > MAX_BYTES:
        raise ParseError('canonical output limit exceeded')
    return raw


def diagnostic(exc, stage):
    """Return fixed error categories, never exception messages or file contents."""
    if isinstance(exc, PermissionError):
        reason = 'permission_denied'
    elif isinstance(exc, FileNotFoundError):
        reason = 'file_missing'
    elif isinstance(exc, FileExistsError):
        reason = 'output_exists'
    elif isinstance(exc, UnicodeDecodeError):
        reason = 'invalid_utf8'
    elif isinstance(exc, (json.JSONDecodeError, KeyError, TypeError)):
        reason = 'invalid_spec'
    elif isinstance(exc, ParseError):
        reason = 'parse_rejected'
    elif isinstance(exc, ImportError):
        reason = 'dependency_unavailable'
    elif isinstance(exc, MemoryError):
        reason = 'memory_error'
    elif isinstance(exc, OSError):
        reason = 'io_error'
    else:
        reason = 'internal_error'
    allowed = {'PermissionError', 'FileNotFoundError', 'FileExistsError', 'UnicodeDecodeError',
               'JSONDecodeError', 'KeyError', 'TypeError', 'ParseError', 'ModuleNotFoundError',
               'ImportError', 'MemoryError', 'OSError'}
    kind = type(exc).__name__
    line = 0
    trace = exc.__traceback__
    while trace is not None:
        if trace.tb_frame.f_code.co_filename == __file__:
            line = trace.tb_lineno
        trace = trace.tb_next
    return {'reason': reason, 'stage': stage, 'exception': kind if kind in allowed else 'other',
            'errno': getattr(exc, 'errno', None) or 0, 'line': line}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True, type=Path)
    p.add_argument('--spec', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    stage = 'read_source'
    try:
        with args.input.open('rb') as f:
            data = f.read(MAX_BYTES + 1)
        stage = 'read_spec'
        spec = json.loads(args.spec.read_text(encoding='utf-8'))
        stage = 'parse'
        raw = parse(data, spec)
        stage = 'write_output'
        with args.output.open('xb') as f:
            f.write(raw)
    except Exception as exc:
        print('DATA_PARSER_DIAGNOSTIC_V1 ' + json.dumps(diagnostic(exc, stage)), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
