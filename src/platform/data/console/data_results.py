"""Readable result views made only from escaped canonical text and known tags."""
import html
from html.parser import HTMLParser
import math
import re
from datetime import datetime


def recognition_summary(value):
    result = value.get('ocr')
    records = result.get('pages') if isinstance(result, dict) else None
    if not isinstance(records, list) or not records:
        return ''
    counts = {status: sum(isinstance(p, dict) and p.get('status') == status for p in records)
              for status in ('completed', 'partial', 'failed', 'skipped')}
    parts = ['완료 ' + str(counts['completed']) + '개 페이지']
    if counts['partial']: parts.append('부분 인식 ' + str(counts['partial']) + '개 페이지')
    if counts['failed']: parts.append('읽기 실패 ' + str(counts['failed']) + '개 페이지')
    if counts['skipped']: parts.append('미처리 ' + str(counts['skipped']) + '개 페이지')
    return '글자 인식 결과: ' + ' · '.join(parts)


def timing_rows(value, operation=None):
    """Optional measurements only; old results must not acquire invented zeros."""
    rows = []
    op = operation or {}
    def duration(first, last):
        try:
            def stamp(raw):
                raw = re.sub(r'(\.\d{6})\d+(?=Z|[+-])', r'\1', raw)
                parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                if parsed.tzinfo is None: raise ValueError()
                return parsed
            seconds = (stamp(last) - stamp(first)).total_seconds()
            return round(seconds, 3) if seconds >= 0 else None
        except (ValueError, TypeError, AttributeError):
            return None
    for label, first, last in (
            ('요청부터 완료까지', 'created_at', 'completed_at'),
            ('실행 전 대기·준비', 'created_at', 'execution_started_at'),
            ('실행 시작부터 완료까지', 'execution_started_at', 'completed_at')):
        seconds = duration(op.get(first), op.get(last))
        if seconds is not None: rows.append({'구간': label, '시간(초)': seconds})
    timing = value.get('timings') or {}
    if timing.get('version') == 'refinement-timing-v1':
        for key, label in (('native_parse_ms', '기본 내용 추출'), ('ocr_ms', '글자 인식 처리'), ('table_comparison_ms', '표 정리'),
                           ('parser_total_ms', '내용 추출·인식 합계')):
            number = timing.get(key)
            if type(number) in (int, float) and math.isfinite(number) and number >= 0:
                rows.append({'구간': label, '시간(초)': round(number / 1000, 3)})
    return rows


def page_timing_rows(value):
    rows = []
    labels = {'completed': '완료', 'partial': '부분 인식', 'failed': '실패', 'skipped': '미처리'}
    for page in (value.get('ocr') or {}).get('pages', []):
        timing = page.get('timings') or {}
        if not timing: continue
        row = {'페이지': page['page'], '결과': labels.get(page.get('status'), '미확인')}
        for key, label in (('render_ms', '이미지 준비(초)'), ('request_ms', '인식 요청·대기(초)'),
                           ('response_parse_ms', '응답 정리(초)'), ('visual_regions_ms', '그림 내부 인식(초)'), ('total_ms', '합계(초)')):
            number = timing.get(key)
            row[label] = round(number / 1000, 3) if type(number) in (int, float) and math.isfinite(number) and number >= 0 else None
        rows.append(row)
    return rows


def cell_text(cell):
    value = cell.get('text', cell.get('value', ''))
    return str(value) if value is not None else ''


def cell_records_html(block):
    """Lossless value/position fallback for sparse or ambiguous table layouts."""
    output = ['<p>표 배치가 크거나 불명확하여 셀별 위치와 값을 표시합니다. 행·열은 0부터 시작합니다.</p>',
              '<table><tr><th>행</th><th>열</th><th>행 병합</th><th>열 병합</th><th>값</th></tr>']
    for cell in block.get('cells', []):
        if not isinstance(cell, dict): continue
        values = [cell.get('row', ''), cell.get('column', ''), cell.get('row_span', 1), cell.get('column_span', 1), cell_text(cell)]
        output.append('<tr>' + ''.join('<td>' + html.escape(str(v)).replace('\n', '<br>') + '</td>' for v in values) + '</tr>')
    output.append('</table>')
    if block.get('merged_ranges'):
        output.append('<p>병합 범위: ' + html.escape(str(block['merged_ranges'])) + '</p>')
    return ''.join(output)


def table_html(block, *, full=False):
    cells = [c for c in block.get('cells', []) if isinstance(c, dict)]
    positions = {}
    clipped = not full and len(cells) > 200
    for cell in cells if full else cells[:200]:
        r, c = cell.get('row'), cell.get('column')
        if type(r) is not int or type(c) is not int or min(r, c) < 0:
            clipped = True
            continue
        if (r, c) in positions:
            clipped = True
            continue
        positions[r, c] = dict(cell)
    if not positions: return cell_records_html(block) if full else '<p>표의 내용을 표시할 수 없습니다.</p>'
    # XLSX records merged ranges separately from cell positions.
    def address(value):
        m = re.fullmatch(r'([A-Z]{1,3})([1-9][0-9]{0,6})', value)
        if not m: raise ValueError()
        col = 0
        for letter in m[1]: col = col * 26 + ord(letter) - 64
        return int(m[2]) - 1, col - 1
    merged = block.get('merged_ranges')
    for area in (merged if full else merged[:200]) if isinstance(merged, list) else []:
        try:
            first, last = area.split(':')
            r, c = address(first); rr, cc = address(last)
            if rr < r or cc < c: raise ValueError()
            if (r, c) in positions:
                positions[r, c].update(row_span=rr-r+1, column_span=cc-c+1)
        except (ValueError, AttributeError):
            clipped = True
    r0, c0 = min(r for r, _ in positions), min(c for _, c in positions)
    extent_r, extent_c = r0, c0
    for (r, c), cell in positions.items():
        rs, cs = cell.get('row_span', 1), cell.get('column_span', 1)
        if type(rs) is not int or type(cs) is not int or rs < 1 or cs < 1:
            rs, cs, clipped = 1, 1, True
        cell['row_span'], cell['column_span'] = rs, cs
        extent_r, extent_c = max(extent_r, r + rs), max(extent_c, c + cs)
    if full and (clipped or (extent_r - r0) * (extent_c - c0) > 250000):
        return cell_records_html(block)
    end_r, end_c = (extent_r, extent_c) if full else (min(extent_r, r0 + 60), min(extent_c, c0 + 20))
    clipped |= end_r != extent_r or end_c != extent_c
    output, covered = ['<table>'], set()
    for r in range(r0, end_r):
        output.append('<tr>')
        for c in range(c0, end_c):
            if (r, c) in covered: continue
            cell = positions.get((r, c), {})
            rs = min(cell.get('row_span', 1), end_r - r)
            cs = min(cell.get('column_span', 1), end_c - c)
            area = {(rr, cc) for rr in range(r, r+rs) for cc in range(c, c+cs)}
            if area & covered or any(cell_text(positions[pos]) for pos in area - {(r, c)} if pos in positions):
                rs, cs, area, clipped = 1, 1, {(r, c)}, True
            covered.update(area)
            text = cell_text(cell)
            clipped |= not full and len(text) > 4000
            tag = 'th' if cell.get('header') else 'td'
            escaped = html.escape(text if full else text[:4000])
            if full: escaped = escaped.replace('\n', '<br>')
            output.append(f'<{tag} rowspan="{rs}" colspan="{cs}">' + escaped + f'</{tag}>')
        output.append('</tr>')
    output.append('</table>')
    if clipped:
        if full: return cell_records_html(block)
        output.append('<p>표의 일부만 표시합니다. 전체 내용은 내려받은 결과에서 확인하세요.</p>')
    return ''.join(output)


def visible_blocks(blocks):
    """Keep readable content; empty images are counted once outside pagination."""
    return [b for b in blocks if isinstance(b, dict) and not empty_image(b)]


def empty_image(block):
    return block.get('type') == 'image' and not block.get('text') and not block.get('cells')


def image_summary(blocks):
    """Count registered empty image regions, not inferred physical illustrations."""
    groups = {}
    for block in blocks:
        if not isinstance(block, dict) or not empty_image(block): continue
        location = block.get('location') or {}
        page, sheet = location.get('page'), location.get('sheet')
        label = str(page) + '페이지' if type(page) is int and page > 0 else str(sheet) if sheet else '위치 미지정'
        groups[label] = groups.get(label, 0) + 1
    if not groups: return ''
    locations = [label + ': ' + str(count) + '개' for label, count in list(groups.items())[:10]]
    if len(groups) > 10: locations.append('외 ' + str(len(groups) - 10) + '곳')
    return '그림 영역 ' + str(sum(groups.values())) + '개 (' + ', '.join(locations) + ')'



def markdown_text(value):
    return re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', html.escape(str(value), quote=False))


class _TableRows(HTMLParser):
    """Read only our escaped table_html output, never raw model markup."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.cell = [], None
    def handle_starttag(self, tag, attrs):
        if tag == 'tr': self.rows.append([])
        if tag in ('td', 'th'):
            attrs = dict(attrs)
            self.cell = [int(attrs.get('rowspan', 1)), int(attrs.get('colspan', 1)), tag == 'th', '']
            self.rows[-1].append(self.cell)
        if tag == 'br' and self.cell is not None: self.cell[3] += '\n'
    def handle_data(self, data):
        if self.cell is not None: self.cell[3] += data
    def handle_endtag(self, tag):
        if tag in ('td', 'th'): self.cell = None


def markdown_table(block):
    markup = table_html(block, full=True)
    def convert(markup, fallback=False):
        parsed = _TableRows(); parsed.feed(markup)
        grid, merged, size = {}, False, 0
        for r, row in enumerate(parsed.rows):
            c = 0
            for rs, cs, header, value in row:
                while (r, c) in grid: c += 1
                size += (len(value.encode('utf-8')) + 8) * rs * cs
                if size > 16 << 20 and not fallback:
                    return convert(cell_records_html(block), fallback=True)
                merged |= rs > 1 or cs > 1
                for rr in range(r, r+rs):
                    for cc in range(c, c+cs): grid[rr, cc] = value
                c += cs
        if not grid: return ''
        width = max(c for _,c in grid)+1
        rows = [[grid.get((r,c), '') for c in range(width)] for r in range(max(r for r,_ in grid)+1)]
        has_header = bool(parsed.rows[0]) and all(c[2] for c in parsed.rows[0]) and all(c[0] == 1 for c in parsed.rows[0])
        header = rows.pop(0) if has_header else ['열 ' + str(c+1) for c in range(width)]
        def line(row): return '| ' + ' | '.join(markdown_text(v).replace('\n', '<br>') for v in row) + ' |'
        output = [line(header), '| ' + ' | '.join('---' for _ in header) + ' |'] + [line(row) for row in rows]
        if merged: output.insert(0, '병합 셀의 값은 해당 행·열에 반복 표시했습니다. 원래 병합 구조는 JSON에 보관됩니다.\n')
        if markup.startswith('<p>'):
            output.insert(0, '표 배치가 크거나 불명확하여 셀별 위치와 값을 표시합니다. 행·열은 0부터 시작합니다.\n')
            if block.get('merged_ranges'): output.append('병합 범위: ' + markdown_text(block['merged_ranges']))
        return '\n'.join(output)
    return convert(markup)


def visual_summary(value):
    records = [r for p in (value.get('ocr') or {}).get('pages', []) for r in p.get('visual_regions', [])]
    if not records: return ''
    complete = sum(r.get('status') == 'completed' for r in records)
    partial = sum(r.get('status') == 'partial' for r in records)
    if partial:
        return f'그림 내부 글자 인식: {complete}개 영역 완료 · {partial}개 영역 부분 인식 · {len(records)-complete-partial}개 영역 미확인'
    return f'그림 내부 글자 인식: {complete}개 영역 완료 · {len(records)-complete}개 영역 미확인'

def reading_html(blocks):
    output, visual = [], None
    for block in visible_blocks(blocks):
        if not isinstance(block, dict): continue
        kind = block.get('type')
        region = block.get('visual_region_id')
        if (region and region != visual) or (kind == 'image' and block.get('text')):
            output.append('<h3>그림에서 읽은 내용</h3>')
        visual = region
        if isinstance(block.get('cells'), list) and block['cells']:
            output.append(table_html(block))
        elif isinstance(block.get('text'), str) and block['text']:
            text = html.escape(block['text'][:20000])
            tag = 'h3' if kind == 'heading' else 'pre' if kind in ('code', 'markdown') else 'p'
            if tag != 'pre': text = text.replace('\n', '<br>')
            output.append('<' + tag + '>' + text + '</' + tag + '>')
            if len(block['text']) > 20000: output.append('<p>긴 내용의 일부를 표시합니다.</p>')
    return ''.join(output) or '<p>표시할 글자나 표가 없습니다. 원본 자료를 확인해주세요.</p>'


def plain_document(value):
    output = [text for text in (processing_summary(value), visual_summary(value)) if text]
    visual = None
    for block in value.get('blocks', []):
        if not isinstance(block, dict): continue
        region = block.get('visual_region_id')
        if (region and region != visual) or (block.get('type') == 'image' and block.get('text')):
            output.append('그림에서 읽은 내용')
        visual = region
        cells = block.get('cells')
        if isinstance(cells, list) and cells:
            rows = {}
            for cell in cells:
                if not isinstance(cell, dict): continue
                r, c = cell.get('row'), cell.get('column')
                if type(r) is int and type(c) is int:
                    rows.setdefault(r, []).append((c, cell_text(cell)))
            output.extend('\t'.join(text for _, text in sorted(rows[r])) for r in sorted(rows))
        elif isinstance(block.get('text'), str): output.append(block['text'])
    return '\n\n'.join(output)


def markdown_document(value):
    """Derived download only; never re-run parsing/OCR or reuse capped previews."""
    def text(value):
        return re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', html.escape(str(value), quote=False))
    output = ['# 정리한 내용']
    mode = processing_summary(value)
    if mode: output.append(text(mode))
    visuals = visual_summary(value)
    if visuals: output.append(text(visuals))
    images = image_summary(value.get('blocks', []))
    if images: output.append(text(images))
    summary = recognition_summary(value)
    if summary: output.append(text(summary))
    notes = quality_messages(value)
    if notes: output.append('## 확인이 필요한 부분\n\n' + '\n'.join('- ' + text(note) for note in notes))
    previous_location, visual = None, None
    for block in visible_blocks(value.get('blocks', [])):
        if not isinstance(block, dict): continue
        location = block.get('location') or {}
        page, sheet = location.get('page'), location.get('sheet')
        group = ('page', page) if type(page) is int and page > 0 else ('sheet', sheet) if sheet else None
        if group and group != previous_location:
            output.append('## ' + (str(page) + '페이지' if group[0] == 'page' else text(sheet)))
            previous_location = group
        kind, body = block.get('type'), block.get('text', '')
        region = block.get('visual_region_id')
        if (region and region != visual) or (kind == 'image' and body):
            output.append('### 그림에서 읽은 내용')
        visual = region
        if block.get('cells'):
            output.append(markdown_table(block))
        elif kind == 'heading' and body:
            level = block.get('level', 2)
            level = min(6, max(1, level)) if type(level) is int else 2
            output.append('#' * level + ' ' + text(body))
        elif kind == 'code' and body:
            fence = '`' * max(3, 1 + max((len(x) for x in re.findall(r'`+', body)), default=0))
            output.append(fence + '\n' + body + '\n' + fence)
        elif kind == 'markdown' and body:
            output.append(html.escape(body))
        elif body:
            output.append(text(body))
    return '\n\n'.join(output) + '\n'


def quality_messages(value):
    groups = {}
    for issue in value.get('quality', {}).get('issues', []):
        if not isinstance(issue, dict): continue
        code = str(issue.get('code', ''))
        if code == 'table_comparison_requires_review':
            text = '표의 배치나 값이 원본과 맞는지 확인해주세요.'
        elif code == 'ocr_visual_content_unread':
            text = '일부 그림·도표 내부의 글자를 읽지 못했습니다. 해당 영역은 원본에서 확인해주세요.'
        elif issue.get('requires_ocr'):
            text = '일부 글자나 그림의 내용을 읽지 못했습니다. 원본과 함께 확인해주세요.'
        elif 'table' in code or 'formula' in code:
            text = '표의 배치나 값이 원본과 맞는지 확인해주세요.'
        elif 'reading_order' in code:
            text = '문단을 읽는 순서가 원본과 맞는지 확인해주세요.'
        elif code.startswith('ocr_'):
            text = '이미지에서 읽은 글자입니다. 숫자와 고유명사를 확인해주세요.'
        else:
            text = '문서의 배치나 일부 내용은 원본과 비교해 확인해주세요.'
        location = issue.get('location')
        page = location.get('page') if isinstance(location, dict) else None
        groups.setdefault(text, set())
        if type(page) is int and page > 0: groups[text].add(page)
    return [text + (' (페이지 ' + ', '.join(map(str, sorted(pages)[:10])) + (' 외' if len(pages) > 10 else '') + ')' if pages else '')
            for text, pages in groups.items()]


def processing_summary(value):
    processing = value.get('processing')
    if not isinstance(processing, dict) or processing.get('version') != 'refinement-processing-v1': return ''
    if processing.get('mode') == 'ocr_priority':
        return '처리 방식: OCR 우선 · 표는 이미지에서 읽은 결과를 우선 사용합니다.'
    if processing.get('mode') == 'parser_only':
        return '처리 방식: 파서만 사용 · OCR을 요청하지 않았습니다.'
    return ''
