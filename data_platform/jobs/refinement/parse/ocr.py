"""Selective Surya v2 inference against a pinned, existing vLLM deployment.

Protocol source: datalab-to/surya c0377481c10527ec8c87bec5a10a6567fdf613fd,
surya/inference/prompts.py and backends/openai_client.py (Apache-2.0).
No SDK autostart, model download, Docker access, external URLs, or POST retries.
Raw generated HTML is retained as JSON data and must never be rendered directly.
"""
import base64
import copy
from collections import Counter
from contextlib import closing
import hashlib
import http.client
import io
import json
import math
import re
import time
import unicodedata
import uuid

from lxml import etree, html
from PIL import Image

PROFILE = 'surya-ocr-v2'
CLIENT_VERSION = 'surya-openai-v1'
PROCESSOR = 'cpu-parser-surya-v1'
MAX_OCR_PAGES = 10
MAX_VISUAL_REGIONS = 8
VISUAL_BUDGET_SECONDS = 120
VISUAL_REQUEST_SECONDS = 45
MAX_PIXELS = 6_000_000
MAX_RESPONSE = 2 << 20
MAX_HTML = 1 << 20
BUDGET_SECONDS = 540
PAGE_SECONDS = 120
# Training-time prompt, retained exactly (24 words).
PROMPT = ('OCR this image to HTML. Each block is a div with data-label and data-bbox '
          '(x0 y0 x1 y1, normalized 0-1000).')


class OCRFailure(Exception):
    def __init__(self, reason, raw_html=''):
        super().__init__(reason)
        self.reason = reason
        self.raw_html = raw_html[:MAX_HTML]


def validate_identity(identity):
    names = {'deployment_id', 'model_version_id', 'runtime_image_id', 'runtime_container_id',
             'internal_endpoint', 'served_model_name', 'profile', 'source_manifest_sha256'}
    if not isinstance(identity, dict) or set(identity) != names:
        raise ValueError('invalid OCR identity')
    for name in ('deployment_id', 'model_version_id'):
        value = identity[name]
        if not isinstance(value, str) or str(uuid.UUID(value)) != value or uuid.UUID(value).int == 0:
            raise ValueError('invalid OCR UUID')
    dep = identity['deployment_id']
    if (identity['profile'] != PROFILE or identity['internal_endpoint'] != f'http://model-serving-{dep}:8000/v1'
            or identity['served_model_name'] != 'deployment-' + dep
            or not re.fullmatch(r'sha256:[a-f0-9]{64}', identity['runtime_image_id'])
            or any(not re.fullmatch(r'[a-f0-9]{64}', identity[k])
                   for k in ('runtime_container_id', 'source_manifest_sha256'))):
        raise ValueError('invalid OCR runtime identity')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate response key')
        result[key] = value
    return result


def reject_constant(_):
    raise ValueError('non-finite response number')


def payload_for(image, identity):
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    png = stream.getvalue()
    if len(png) > 8 << 20:
        raise OCRFailure('render_bytes_limit')
    request = {'model': identity['served_model_name'], 'stream': False, 'temperature': 0,
               'top_p': 0.1, 'max_tokens': 12288, 'logprobs': True,
               'messages': [{'role': 'user', 'content': [
                   {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')}},
                   {'type': 'text', 'text': PROMPT}]}]}
    return json.dumps(request, separators=(',', ':')).encode(), hashlib.sha256(png).hexdigest()


def decode_response(data, model):
    try:
        body = json.loads(data, object_pairs_hook=unique_object, parse_constant=reject_constant)
        choices = body['choices']
        if body['model'] != model or len(choices) != 1 or choices[0]['message']['role'] != 'assistant':
            raise ValueError('response identity')
        choice = choices[0]
        text = choice['message']['content']
        if not isinstance(text, str) or len(text.encode()) > MAX_HTML:
            raise ValueError('response content')
        if choice.get('finish_reason') != 'stop':
            raise OCRFailure('generation_incomplete', text)
        if not text.strip():
            raise OCRFailure('empty_response')
        content = (choice.get('logprobs') or {}).get('content') or []
        if not isinstance(content, list) or len(content) > 20000:
            raise ValueError('logprob size')
        values = []
        for token in content:
            value = token.get('logprob')
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value > 0:
                raise ValueError('logprob value')
            values.append(math.exp(value))
        confidence = sum(values) / len(values) if values else None
        usage = body.get('usage') or {}
        safe_usage = {}
        for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            value = usage.get(key)
            if value is not None:
                if type(value) is not int or value < 0:
                    raise ValueError('usage')
                safe_usage[key] = value
        return {'raw_html': text, 'page_mean_token_probability': confidence, 'usage': safe_usage, 'finish_reason': 'stop'}
    except OCRFailure:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
        raise OCRFailure('invalid_response') from None


def infer(image, identity, deadline):
    validate_identity(identity)
    payload, image_sha = payload_for(image, identity)
    deadline = min(deadline, time.monotonic() + PAGE_SECONDS)
    left = deadline - time.monotonic()
    if left <= 0:
        raise OCRFailure('time_budget_exceeded')
    # HTTPConnection has no ambient proxy support and does not follow redirects.
    conn = http.client.HTTPConnection('model-serving-' + identity['deployment_id'], 8000, timeout=left)
    try:
        conn.request('POST', '/v1/chat/completions', payload, {'Content-Type': 'application/json'})
        response = conn.getresponse()
        if response.status != 200:
            raise OCRFailure('runtime_http_error')
        chunks, size = [], 0
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise OCRFailure('time_budget_exceeded')
            if conn.sock is not None:
                conn.sock.settimeout(left)
            chunk = response.read1(min(16384, MAX_RESPONSE + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_RESPONSE:
                raise OCRFailure('response_bytes_limit')
        result = decode_response(b''.join(chunks), identity['served_model_name'])
        result['input_png_sha256'] = image_sha
        return result
    except OCRFailure:
        raise
    except TimeoutError:
        raise OCRFailure('timeout') from None
    except (OSError, http.client.HTTPException):
        raise OCRFailure('runtime_unavailable') from None
    finally:
        conn.close()


def fit_size(width, height):
    if width <= 0 or height <= 0 or not math.isfinite(width * height):
        raise OCRFailure('render_dimensions_invalid')
    scale = min(1, math.sqrt(MAX_PIXELS / (width * height)), 4096 / max(width, height))
    return max(1, int(width * scale)), max(1, int(height * scale))


def render_page(data, kind, page):
    if kind in ('png', 'jpeg'):
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > 40_000_000:
                raise OCRFailure('source_pixels_limit')
            target = fit_size(*source.size)
            source.thumbnail(target)
            # Preserve the source's pixel coordinate convention (no EXIF rotate).
            if source.mode == 'RGBA' or 'transparency' in source.info:
                rgba = source.convert('RGBA')
                image = Image.new('RGB', rgba.size, 'white')
                image.paste(rgba, mask=rgba.getchannel('A'))
                rgba.close()
            else:
                image = source.convert('RGB')
        return image
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(data) as pdf:
        with closing(pdf[page['number'] - 1]) as source:
            width, height = source.get_size()
            # Different crop/rotation conventions must not silently mis-map boxes.
            if abs(width - page['width']) > 1 or abs(height - page['height']) > 1:
                raise OCRFailure('pdf_coordinate_mismatch')
            rotation = source.get_rotation()
            if rotation != page.get('rotation', 0):
                raise OCRFailure('pdf_coordinate_mismatch')
            # get_bbox inherits page-tree boxes and returns MediaBox intersected
            # with CropBox in unrotated canvas units. Matching dimensions alone
            # misses a shifted origin. Canonical OCR boxes use the full rotated
            # page with a zero origin; unsupported viewports retain native data.
            canvas_width, canvas_height = ((page['height'], page['width']) if rotation in (90, 270)
                                            else (page['width'], page['height']))
            expected_box = (0, 0, canvas_width, canvas_height)
            if any(not math.isfinite(a) or abs(a - b) > 0.01 for a, b in zip(source.get_bbox(), expected_box)):
                raise OCRFailure('pdf_coordinate_mismatch')
            target = fit_size(width * 192 / 72, height * 192 / 72)
            scale = min(target[0] / width, target[1] / height)
            with closing(source.render(scale=scale, rev_byteorder=True)) as bitmap:
                return bitmap.to_pil().convert('RGB').copy()


ALLOWED_TAGS = {'math', 'br', 'i', 'b', 'u', 'del', 'sup', 'sub', 'table', 'tr', 'td', 'p', 'th',
                'div', 'pre', 'h1', 'h2', 'h3', 'h4', 'h5', 'ul', 'ol', 'li', 'span', 'hr', 'tbody',
                'small', 'caption', 'strong', 'thead', 'big', 'code', 'chem'}
FORBIDDEN_TAGS = {'script', 'style', 'iframe', 'object', 'embed', 'svg', 'link', 'meta', 'base'}
LABEL_TYPES = {'Text': 'paragraph', 'Caption': 'paragraph', 'Footnote': 'paragraph',
               'List-Group': 'paragraph', 'Page-Header': 'paragraph', 'Page-Footer': 'paragraph',
               'Section-Header': 'heading', 'Table': 'table', 'Code-Block': 'code',
               'Image': 'image', 'Figure': 'image', 'Diagram': 'image'}


def plain_text(node):
    parts = [node.text or '']
    for child in node:
        if isinstance(child.tag, str):
            if child.tag == 'br':
                parts.append('\n')
            else:
                parts.append(plain_text(child))
                if child.tag in ('p', 'div', 'tr', 'li'):
                    parts.append('\n')
            parts.append(child.tail or '')
    return ''.join(parts)


def sanitize(node):
    cloned = copy.deepcopy(node)
    changed = False
    for child in list(cloned.iter()):
        if not isinstance(child.tag, str):
            parent = child.getparent()
            if parent is not None:
                parent.remove(child)
            changed = True
            continue
        if child.tag.lower() in FORBIDDEN_TAGS:
            raise OCRFailure('unsafe_html')
        if child.tag not in ALLOWED_TAGS:
            child.tag = 'span'
            changed = True
        for key in list(child.attrib):
            if key not in ('colspan', 'rowspan', 'display'):
                del child.attrib[key]
                changed = True
    return cloned, changed


def table_cells(table):
    if table.xpath('.//table'):
        raise OCRFailure('nested_table_geometry')
    rows = table.xpath('./tr|./thead/tr|./tbody/tr|./tfoot/tr')
    if len(rows) > 1000:
        raise OCRFailure('table_limit')
    occupied, result = set(), []
    for row, element in enumerate(rows):
        col = 0
        for cell in element:
            if cell.tag not in ('td', 'th'):
                continue
            while (row, col) in occupied:
                col += 1
            try:
                rs, cs = int(cell.get('rowspan', '1')), int(cell.get('colspan', '1'))
            except ValueError:
                raise OCRFailure('invalid_table_span') from None
            if rs < 1 or cs < 1 or row + rs > len(rows) or col + cs > 1000 or len(occupied) + rs * cs > 20000:
                raise OCRFailure('invalid_table_span')
            covered = {(r, c) for r in range(row, row + rs) for c in range(col, col + cs)}
            if occupied & covered:
                raise OCRFailure('overlapping_table_cells')
            occupied.update(covered)
            result.append({'row': row, 'column': col, 'row_span': rs, 'column_span': cs,
                           'text': plain_text(cell), 'header': cell.tag == 'th'})
            col += cs
    if not result:
        raise OCRFailure('empty_table')
    return result


def parse_blocks(raw, page):
    cleaned = raw.strip()
    if cleaned.startswith('```'):
        cleaned = re.sub(r'^```(?:html)?\s*\n', '', cleaned)
        cleaned = re.sub(r'\n```\s*$', '', cleaned)
    try:
        root = html.fromstring('<html><body>' + cleaned + '</body></html>',
                               parser=html.HTMLParser(recover=False, no_network=True))
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        raise OCRFailure('invalid_html', raw) from None
    bodies = root.xpath('//body')
    if len(bodies) != 1:
        raise OCRFailure('invalid_html', raw)
    body = bodies[0]
    if not len(body) or len(body) > 500 or (body.text or '').strip():
        raise OCRFailure('invalid_block_structure', raw)
    blocks, issues = [], set()
    for order, node in enumerate(body):
        if node.tag != 'div' or (node.tail or '').strip():
            raise OCRFailure('invalid_block_structure', raw)
        label = node.get('data-label', '')
        values = node.get('data-bbox', '').replace(',', ' ').split()
        try:
            bbox = [float(x) for x in values]
        except ValueError:
            raise OCRFailure('invalid_bbox', raw) from None
        if (len(bbox) != 4 or any(not math.isfinite(x) or not 0 <= x <= 1000 for x in bbox)
                or bbox[0] >= bbox[2] or bbox[1] >= bbox[3] or not label or len(label) > 64):
            raise OCRFailure('invalid_bbox', raw)
        if node.xpath('.//*[@data-bbox]'):
            raise OCRFailure('nested_block_coordinates', raw)
        # The wrapper's metadata belongs in JSON, not in renderable HTML.
        node.attrib.clear()
        try:
            safe, filtered = sanitize(node)
            tables = safe.xpath('.//table')
            kind = LABEL_TYPES.get(label, 'unhandled_structure')
            if kind == 'table' and len(tables) != 1:
                raise OCRFailure('table_structure_missing')
            if tables and kind != 'table':
                issues.add('ocr_nested_table_requires_review')
            block = {'type': kind, 'text': plain_text(safe), 'html': etree.tostring(safe, encoding='unicode', method='html'),
                     'label': label, 'extraction': CLIENT_VERSION, 'reading_order': order,
                     'location': {'page': page['number'], 'coordinate_system': page['coordinate_system'],
                                  'bbox': [bbox[0] * page['width'] / 1000, bbox[1] * page['height'] / 1000,
                                           bbox[2] * page['width'] / 1000, bbox[3] * page['height'] / 1000]},
                     'model_bbox': bbox, 'model_coordinate_system': 'normalized-0-1000-top-left'}
            if kind == 'table':
                block['cells'] = table_cells(tables[0])
                issues.add('ocr_table_cell_geometry_unavailable')
            if kind == 'unhandled_structure':
                issues.add('ocr_label_requires_review')
            if filtered:
                issues.add('ocr_markup_filtered')
            if not block['text'].strip() and label != 'Blank-Page':
                if kind != 'image':
                    raise OCRFailure('empty_block_text')
                # A visual region without a transcription is valid geometry,
                # not evidence that the rest of the page is unreadable.
                block['recognition_status'] = 'unread'
            blocks.append(block)
        except OCRFailure as exc:
            raise OCRFailure(exc.reason, raw) from None
    return blocks, sorted(issues)



def render_visual(image, page, location):
    """Crop only the verified page raster; return its actual source coordinates."""
    box = location.get('bbox', [])
    if (location.get('page') != page['number'] or location.get('coordinate_system') != page['coordinate_system']
            or len(box) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in box)
            or not 0 <= box[0] < box[2] <= page['width'] or not 0 <= box[1] < box[3] <= page['height']):
        raise OCRFailure('visual_geometry_invalid')
    x0, y0 = math.floor(box[0] / page['width'] * image.width), math.floor(box[1] / page['height'] * image.height)
    x1, y1 = math.ceil(box[2] / page['width'] * image.width), math.ceil(box[3] / page['height'] * image.height)
    if x1 - x0 < 2 or y1 - y0 < 2:
        raise OCRFailure('visual_region_too_small')
    crop = image.crop((x0, y0, x1, y1))
    scale = min(3, max(1, 1600 / max(crop.size)))
    target = fit_size(crop.width * scale, crop.height * scale)
    if target != crop.size:
        resized = crop.resize(target, Image.Resampling.LANCZOS)
        crop.close()
        crop = resized
    actual = [x0 * page['width'] / image.width, y0 * page['height'] / image.height,
              x1 * page['width'] / image.width, y1 * page['height'] / image.height]
    return crop, dict(location, bbox=actual)


def same_visual_text(left, right):
    """Exact prose and near-identical source area; never merge table/code cells."""
    if (left.get('type') != 'paragraph' or right.get('type') != 'paragraph'
            or left.get('label') != right.get('label')
            or left.get('extraction') != 'surya-openai-v1' or right.get('extraction') != 'surya-openai-v1'
            or left.get('recognition_status') or right.get('recognition_status')):
        return False
    text = lambda b: ''.join(unicodedata.normalize('NFC', b.get('text', '')).split())
    if not text(left) or text(left) != text(right):
        return False
    a, b = left.get('location', {}), right.get('location', {})
    if a.get('page') != b.get('page') or a.get('coordinate_system') != b.get('coordinate_system'):
        return False
    a, b = a.get('bbox'), b.get('bbox')
    if any(not isinstance(v, list) or len(v) != 4 or
           any(type(n) not in (int, float) or not math.isfinite(n) for n in v) or
           v[0] >= v[2] or v[1] >= v[3] for v in (a, b)):
        return False
    areas = [(v[2]-v[0])*(v[3]-v[1]) for v in (a, b)]
    intersection = max(0, min(a[2], b[2])-max(a[0], b[0])) * max(0, min(a[3], b[3])-max(a[1], b[1]))
    return intersection / min(areas) >= 0.9 and min(areas) / max(areas) >= 0.5


def deduplicate_visual_text(blocks):
    """Keep crop provenance/counts and remove only unambiguous page duplicates.

    Distant repeated text, partial matches, changed numbers and one-to-many
    matches remain untouched. Store the removed page block as audit evidence.
    """
    pages = [i for i, b in enumerate(blocks) if not b.get('visual_region_id') and b.get('type') == 'paragraph']
    crops = [i for i, b in enumerate(blocks) if b.get('visual_region_id') and b.get('type') == 'paragraph']
    pairs = [(p, c) for p in pages for c in crops if same_visual_text(blocks[p], blocks[c])]
    page_counts, crop_counts = Counter(p for p, _ in pairs), Counter(c for _, c in pairs)
    removed, evidence = set(), []
    for p, c in pairs:
        if page_counts[p] != 1 or crop_counts[c] != 1:
            continue
        removed.add(p)
        evidence.append({'reason': 'same_text_and_region',
                         'removed': copy.deepcopy({k: v for k, v in blocks[p].items() if k not in ('id', 'order')}),
                         'kept_visual_region_id': blocks[c]['visual_region_id'],
                         'kept_model_bbox': copy.deepcopy(blocks[c]['model_bbox'])})
    return [b for i, b in enumerate(blocks) if i not in removed], evidence


def enrich_visual_regions(doc, data, identity, replacements, infer_page, renderer, deadline, measure_timings):
    """One bounded crop request per unread visual, after all page requests finish."""
    deadline = min(deadline, time.monotonic() + VISUAL_BUDGET_SECONDS)
    attempts, sequence = 0, 0
    pages = {p['number']: p for p in doc.value['pages']}
    for record in doc.value['ocr']['pages']:
        number = record['page']
        blocks = replacements.get(number)
        if not blocks or not any(b.get('recognition_status') == 'unread' for b in blocks):
            continue
        page, raster = pages[number], None
        combined, records = [], []
        record['visual_regions'] = records
        started = time.monotonic()
        try:
            for block in blocks:
                if block.get('recognition_status') != 'unread':
                    combined.append(block)
                    continue
                sequence += 1
                region = {'id': 'v' + str(sequence), 'status': 'failed', 'reason': '',
                          'source_location': copy.deepcopy(block['location']), 'source_label': block['label'],
                          'result_blocks': 0}
                records.append(region)
                crop, region_started = None, time.monotonic()
                try:
                    if attempts >= MAX_VISUAL_REGIONS or time.monotonic() >= deadline:
                        region['status'] = 'skipped'
                        raise OCRFailure('visual_budget_exceeded')
                    attempts += 1
                    if raster is None:
                        raster = renderer(data, doc.value['source']['format'], page)
                    crop, location = render_visual(raster, page, block['location'])
                    region.update(location=location, rendered_image={'width': crop.width, 'height': crop.height,
                                                                     'coordinate_system': 'image-pixels-top-left'})
                    answer = infer_page(crop, identity, min(deadline, time.monotonic() + VISUAL_REQUEST_SECONDS))
                    region.update(answer)
                    confidence = answer['page_mean_token_probability']
                    if confidence is not None and confidence < 0.5:
                        raise OCRFailure('low_token_probability', answer['raw_html'])
                    x0, y0, x1, y1 = location['bbox']
                    children, issues = parse_blocks(answer['raw_html'], dict(page, width=x1-x0, height=y1-y0))
                    children = [b for b in children if b['label'] != 'Blank-Page']
                    # No recursive crop requests or fabricated success for empty pictures.
                    unread_children = any(b.get('recognition_status') == 'unread' for b in children)
                    children = [b for b in children if b.get('recognition_status') != 'unread']
                    if not any(b.get('text', '').strip() for b in children):
                        raise OCRFailure('visual_text_unread', answer['raw_html'])
                    if any('\ufffd' in b.get('text', '') for b in children):
                        raise OCRFailure('invalid_text_quality', answer['raw_html'])
                    for child in children:
                        box = child['location']['bbox']
                        child['location']['bbox'] = [box[0]+x0, box[1]+y0, box[2]+x0, box[3]+y0]
                        child['visual_region_id'] = region['id']
                        child['model_coordinate_system'] = 'normalized-0-1000-crop-top-left'
                    region.update(status='partial' if unread_children else 'completed',
                                  reason='visual_text_unread' if unread_children else '', result_blocks=len(children))
                    if unread_children:
                        # Keep the original unread extent; child pictures are not
                        # recursively cropped and cannot hide missing content.
                        combined.append(block)
                    combined.extend(children)
                    for issue in issues:
                        doc.issue(issue, {'page': number})
                    if confidence is None:
                        doc.issue('ocr_token_probability_unavailable', {'page': number})
                    doc.issue('ocr_visual_region_output_requires_review', {'page': number})
                except OCRFailure as exc:
                    region['reason'] = exc.reason
                    if exc.raw_html: region['raw_html'] = exc.raw_html
                    combined.append(block)
                except Exception:
                    region['reason'] = 'visual_render_or_client_error'
                    combined.append(block)
                finally:
                    if crop is not None: crop.close()
                    if measure_timings: region['total_ms'] = round((time.monotonic()-region_started)*1000, 3)
        finally:
            if raster is not None: raster.close()
            if measure_timings:
                elapsed = round((time.monotonic()-started)*1000, 3)
                record['timings']['visual_regions_ms'] = elapsed
                record['timings']['total_ms'] += elapsed
        combined, duplicates = deduplicate_visual_text(combined)
        if duplicates:
            record['deduplicated_page_blocks'] = duplicates
            doc.issue('ocr_page_visual_duplicate_suppressed', {'page': number})
        unread = [b for b in combined if b.get('recognition_status') == 'unread']
        doc.value['quality']['issues'] = [i for i in doc.value['quality']['issues']
            if not (i['code'] == 'ocr_visual_content_unread' and i.get('location', {}).get('page') == number)]
        if not any(b.get('text', '').strip() for b in combined):
            # A picture-only response is still a failed page unless its crop reads text.
            del replacements[number]
            record.update(status='failed', reason='no_readable_blocks')
            record.pop('unresolved_visual_blocks', None)
            doc.value['quality']['issues'] = [i for i in doc.value['quality']['issues']
                if i.get('location', {}).get('page') != number] + copy.deepcopy(record['native_issues'])
            doc.issue('ocr_no_readable_blocks', {'page': number}, ocr=True)
            continue
        replacements[number] = combined
        record.update(status='partial' if unread else 'completed', reason='visual_content_unread' if unread else '')
        if unread: record['unresolved_visual_blocks'] = len(unread)
        else: record.pop('unresolved_visual_blocks', None)
        for block in unread: doc.issue('ocr_visual_content_unread', block['location'], ocr=True)

def enrich(doc, data, identity, infer_page=None, renderer=None, measure_timings=False):
    validate_identity(identity)
    kind = doc.value['source']['format']
    if kind not in ('png', 'jpeg', 'pdf'):
        raise ValueError('unsupported OCR source format')
    infer_page = infer_page or infer
    renderer = renderer or render_page
    original = copy.deepcopy(doc.value)
    selected = {issue.get('location', {}).get('page') for issue in original['quality']['issues']
                if issue.get('requires_ocr') or issue['code'] == 'pdf_table_spans_and_borderless_tables_require_review'}
    selected.discard(None)
    doc.value['processor']['version'] = PROCESSOR
    doc.value['ocr'] = {'identity': copy.deepcopy(identity), 'client_version': CLIENT_VERSION,
                        'policy': 'selective-pages-v1', 'pages': []}
    replacements = {}
    deadline = time.monotonic() + BUDGET_SECONDS
    for index, page in enumerate(p for p in original['pages'] if p['number'] in selected):
        number = page['number']
        native = [b for b in original['blocks'] if b.get('location', {}).get('page') == number]
        native_issues = [i for i in original['quality']['issues'] if i.get('location', {}).get('page') == number]
        record = {'page': number, 'status': 'failed', 'reason': '', 'native_blocks': native, 'native_issues': native_issues}
        doc.value['ocr']['pages'].append(record)
        image = None
        page_started = time.monotonic()
        timings = {}
        stage = None
        stage_started = page_started
        try:
            if index >= MAX_OCR_PAGES:
                record['status'] = 'skipped'
                raise OCRFailure('page_budget_exceeded')
            if time.monotonic() >= deadline:
                record['status'] = 'skipped'
                raise OCRFailure('time_budget_exceeded')
            stage, stage_started = 'render_ms', time.monotonic()
            image = renderer(data, kind, page)
            timings[stage] = round((time.monotonic() - stage_started) * 1000, 3)
            record['rendered_image'] = {'width': image.width, 'height': image.height,
                                        'coordinate_system': 'image-pixels-top-left'}
            stage, stage_started = 'request_ms', time.monotonic()
            answer = infer_page(image, identity, deadline)
            timings[stage] = round((time.monotonic() - stage_started) * 1000, 3)
            record.update(answer)
            stage, stage_started = 'response_parse_ms', time.monotonic()
            blocks, issues = parse_blocks(answer['raw_html'], page)
            timings[stage] = round((time.monotonic() - stage_started) * 1000, 3)
            confidence = answer['page_mean_token_probability']
            if confidence is not None and confidence < 0.5:
                raise OCRFailure('low_token_probability', answer['raw_html'])
            if any('\ufffd' in b.get('text', '') for b in blocks):
                raise OCRFailure('invalid_text_quality', answer['raw_html'])
            unread = [b for b in blocks if b.get('recognition_status') == 'unread']
            replacements[number] = blocks
            record['status'] = 'partial' if unread else 'completed'
            if unread:
                record['reason'] = 'visual_content_unread'
                record['unresolved_visual_blocks'] = len(unread)
            doc.value['quality']['issues'] = [i for i in doc.value['quality']['issues']
                                             if i.get('location', {}).get('page') != number]
            doc.issue('ocr_output_requires_review', {'page': number})
            for block in unread:
                doc.issue('ocr_visual_content_unread', block['location'], ocr=True)
            if confidence is None:
                doc.issue('ocr_token_probability_unavailable', {'page': number})
            for issue in issues:
                doc.issue(issue, {'page': number})
        except OCRFailure as exc:
            record['reason'] = exc.reason
            if exc.raw_html:
                record['raw_html'] = exc.raw_html
            doc.issue('ocr_' + exc.reason, {'page': number}, ocr=True)
        except Exception:
            # Keep original extraction when native image/PDF decoding fails.
            # Never leak source paths or third-party exception text into logs.
            record['reason'] = 'render_or_client_error'
            doc.issue('ocr_render_or_client_error', {'page': number}, ocr=True)
        finally:
            if measure_timings:
                if stage is not None and stage not in timings:
                    timings[stage] = round((time.monotonic() - stage_started) * 1000, 3)
                timings['total_ms'] = round((time.monotonic() - page_started) * 1000, 3)
                record['timings'] = timings
            if image is not None:
                image.close()
    enrich_visual_regions(doc, data, identity, replacements, infer_page, renderer, deadline, measure_timings)
    merged = []
    # All supported inputs have page-scoped blocks. Preserve native blocks for
    # untouched/failed pages and replace successful pages in document order.
    for page in original['pages']:
        number = page['number']
        merged.extend(replacements.get(number, [b for b in original['blocks'] if b.get('location', {}).get('page') == number]))
    for index, block in enumerate(merged):
        block['id'], block['order'] = 'b' + str(index + 1), index
    doc.value['blocks'] = merged
    if replacements:
        doc.value['quality']['issues'] = [i for i in doc.value['quality']['issues'] if i['code'] != 'no_content_extracted']
    issues = doc.value['quality']['issues']
    doc.value['quality']['state'] = 'needs_ocr' if any(i['requires_ocr'] for i in issues) else ('needs_review' if issues else 'ready')
