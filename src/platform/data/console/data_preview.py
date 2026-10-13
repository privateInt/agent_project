"""Bounded, read-only previews of verified bytes. No source URLs are fetched."""
import base64
import html
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile

MAX_INPUT = 32 << 20
MAX_OUTPUT = 8 << 20
MAX_TEXT = 200000


class PreviewError(ValueError):
    pass


def decode_text(raw, content_type=''):
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw.decode('utf-16', errors='replace')
    match = re.search(r'charset\s*=\s*["\']?([\w-]+)', content_type, re.I)
    encoding = match.group(1).lower() if match else 'utf-8-sig'
    if encoding not in ('utf-8', 'utf-8-sig', 'ascii', 'euc-kr', 'cp949', 'iso-8859-1', 'windows-1252'):
        encoding = 'utf-8-sig'
    return raw.decode(encoding, errors='replace')


class ReadingHTML(HTMLParser):
    """Reconstruct text structure with a closed set of tags and zero attributes."""
    tags = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'ul', 'ol', 'li', 'table',
            'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'blockquote', 'pre', 'code',
            'strong', 'b', 'em', 'i', 'br', 'hr'}
    hidden = {'head', 'script', 'style', 'iframe', 'object', 'embed', 'noscript',
              'template', 'svg', 'math', 'form', 'textarea', 'select'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.stack, self.skipped = [], [], []
        self.title, self.in_title = [], False
        self.count, self.truncated = 0, False

    def handle_starttag(self, tag, attrs):
        if tag == 'title': self.in_title = True
        if tag in self.hidden:
            # embed is void; do not hide the rest of the document after it.
            if tag != 'embed': self.skipped.append(tag)
            return
        if self.skipped or self.count >= MAX_TEXT or len(self.parts) >= 10000:
            return
        if tag == 'img':
            alt = dict(attrs).get('alt')
            if alt:
                self.parts.append('<p>[이미지: ' + html.escape(alt[:300]) + ']</p>')
        elif tag in self.tags:
            self.parts.append('<' + tag + '>')
            if tag not in ('br', 'hr'): self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == 'title': self.in_title = False
        if self.skipped:
            if tag in self.skipped:
                self.skipped = self.skipped[:len(self.skipped) - 1 - self.skipped[::-1].index(tag)]
            return
        if tag in self.stack:
            while self.stack:
                top = self.stack.pop()
                self.parts.append('</' + top + '>')
                if top == tag: break

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data[:200])
        if self.skipped:
            return
        if self.count >= MAX_TEXT or len(self.parts) >= 10000:
            self.truncated = True
            return
        text = data[:MAX_TEXT - self.count]
        self.count += len(text)
        self.truncated |= len(text) != len(data)
        self.parts.append(html.escape(text))

    def result(self):
        return {'kind': 'html', 'html': ''.join(self.parts) + ''.join('</' + t + '>' for t in reversed(self.stack)),
                'title': ' '.join(self.title)[:300], 'truncated': self.truncated}


def html_preview(raw, content_type=''):
    parser = ReadingHTML()
    clipped = raw[:2 << 20]
    parser.feed(decode_text(clipped, content_type))
    result = parser.result()
    result['truncated'] |= len(raw) != len(clipped)
    return result


def png_payload(image):
    image.thumbnail((1200, 1200))
    buf = io.BytesIO()
    image.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode('ascii')


def image_preview(raw):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 16000000
    with Image.open(io.BytesIO(raw)) as image:
        if image.width * image.height > 16000000:
            raise PreviewError('이미지가 미리보기 크기 한도를 초과합니다.')
        width, height = image.size
        return {'kind': 'image', 'png': png_payload(ImageOps.exif_transpose(image).convert('RGB')),
                'width': width, 'height': height}


def pdf_preview(raw, page, region=None):
    import math
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(raw) as pdf:
        count = len(pdf)
        if not 0 <= page < min(count, 300):
            raise PreviewError('미리보기 페이지 범위를 벗어났습니다.')
        pdf_page = pdf[page]
        try:
            width, height = pdf_page.get_size()
            if not all(math.isfinite(x) and x > 0 for x in (width, height)):
                raise PreviewError('PDF 페이지 크기가 올바르지 않습니다.')
            if region is not None:
                bbox = region.get('bbox')
                if (not isinstance(bbox, list) or len(bbox) != 4 or
                        any(type(v) not in (int, float) or not math.isfinite(v) for v in bbox) or
                        not 0 <= bbox[0] < bbox[2] <= width or not 0 <= bbox[1] < bbox[3] <= height or
                        abs(region.get('width', 0)-width) > 0.01 or abs(region.get('height', 0)-height) > 0.01 or
                        region.get('rotation') != pdf_page.get_rotation()):
                    raise PreviewError('원본과 표의 좌표가 맞지 않아 영역 미리보기를 표시하지 않습니다. 원본 파일을 확인해주세요.')
                expected = (0, 0, height, width) if pdf_page.get_rotation() in (90, 270) else (0, 0, width, height)
                if any(abs(a-b) > 0.01 for a, b in zip(pdf_page.get_bbox(), expected)):
                    raise PreviewError('원본 페이지의 잘림 영역 때문에 표의 위치를 확인할 수 없습니다. 원본 파일을 확인해주세요.')
            bitmap = pdf_page.render(scale=min(1.5, 1200 / max(width, height)), may_draw_forms=False)
            try:
                image = bitmap.to_pil().convert('RGB')
                try:
                    if region is not None:
                        bbox = region['bbox']
                        crop = (max(0, int(bbox[0]/width*image.width)), max(0, int(bbox[1]/height*image.height)),
                                min(image.width, math.ceil(bbox[2]/width*image.width)), min(image.height, math.ceil(bbox[3]/height*image.height)))
                        clipped = image.crop(crop)
                        image.close()
                        image = clipped
                    png = png_payload(image)
                finally:
                    image.close()
                return {'kind': 'pdf', 'png': png,
                        'page_count': count, 'page': page, 'truncated': count > 300}
            finally:
                bitmap.close()
        finally:
            pdf_page.close()


class OfficePackage:
    def __init__(self, raw):
        self.zip = zipfile.ZipFile(io.BytesIO(raw))
        infos = self.zip.infolist()
        if len(infos) > 4096 or sum(i.file_size for i in infos) > 64 << 20:
            self.zip.close()
            raise PreviewError('문서 압축 해제 크기가 미리보기 한도를 초과합니다.')

    def xml(self, name):
        import xml.etree.ElementTree as ET
        info = self.zip.getinfo(name)
        if info.file_size > 8 << 20 or info.flag_bits & 1:
            raise PreviewError('암호화되었거나 큰 문서입니다.')
        with self.zip.open(name) as f:
            raw = f.read((8 << 20) + 1)
        ascii_xml = raw.replace(b'\x00', b'').upper()
        if len(raw) > 8 << 20 or b'<!DOCTYPE' in ascii_xml or b'<!ENTITY' in ascii_xml:
            raise PreviewError('미리보기가 지원하지 않는 XML입니다.')
        return ET.fromstring(raw)


def word_preview(raw):
    package = OfficePackage(raw)
    try:
        root = package.xml('word/document.xml')
        body = root.find('{*}body')
        if body is None: raise PreviewError('문서 본문이 없습니다.')
        output, truncated = [], False
        for node in list(body)[:400]:
            tag = node.tag.rsplit('}', 1)[-1]
            if tag == 'p':
                text = ''.join(t.text or '' for t in node.findall('.//{*}t'))
                output.append('<p>' + html.escape(text[:5000]) + '</p>')
                truncated |= len(text) > 5000
            elif tag == 'tbl':
                output.append('<table>')
                rows = node.findall('{*}tr')
                for row in rows[:100]:
                    output.append('<tr>')
                    cells = row.findall('{*}tc')
                    truncated |= len(cells) > 30
                    for cell in cells[:30]:
                        text = '\n'.join(''.join(t.text or '' for t in p.findall('.//{*}t')) for p in cell.findall('{*}p'))
                        output.append('<td>' + html.escape(text[:3000]) + '</td>')
                        truncated |= len(text) > 3000
                    output.append('</tr>')
                output.append('</table>')
                truncated |= len(rows) > 100
            if sum(len(p) for p in output) > MAX_TEXT:
                truncated = True
                break
        return {'kind': 'docx', 'html': ''.join(output), 'truncated': truncated or len(body) > 400}
    finally:
        package.zip.close()


def sheet_preview(raw, index):
    import posixpath
    package = OfficePackage(raw)
    try:
        book = package.xml('xl/workbook.xml')
        all_sheets = book.findall('{*}sheets/{*}sheet')
        sheets = all_sheets[:100]
        truncated = len(all_sheets) > 100
        if not 0 <= index < len(sheets): raise PreviewError('시트가 없습니다.')
        names = [s.attrib.get('name', '시트')[:200] for s in sheets]
        rels = package.xml('xl/_rels/workbook.xml.rels')
        rid = next((v for k, v in sheets[index].attrib.items() if k.endswith('}id')), '')
        rel = next((x for x in rels if x.attrib.get('Id') == rid), None)
        if rel is None or rel.attrib.get('TargetMode') == 'External':
            raise PreviewError('시트 위치를 확인할 수 없습니다.')
        target = rel.attrib.get('Target', '')
        path = posixpath.normpath('xl/' + target) if not target.startswith('/') else target[1:]
        if not path.startswith('xl/') or '\\' in path:
            raise PreviewError('지원하지 않는 시트 경로입니다.')
        strings = []
        if 'xl/sharedStrings.xml' in package.zip.namelist():
            strings = [''.join(t.text or '' for t in s.findall('.//{*}t')) for s in package.xml('xl/sharedStrings.xml')]
        sheet = package.xml(path)
        rows, formulas = [], []
        all_rows = sheet.findall('{*}sheetData/{*}row')
        for row in all_rows[:100]:
            values = {}
            for cell in row.findall('{*}c')[:50]:
                address = cell.attrib.get('r', '')
                column = re.sub('[0-9]', '', address)[:5]
                value = cell.findtext('{*}v', default='')
                if cell.attrib.get('t') == 's':
                    value = strings[int(value)]
                elif cell.attrib.get('t') == 'inlineStr':
                    value = ''.join(t.text or '' for t in cell.findall('.//{*}t'))
                formula = cell.findtext('{*}f')
                if formula is not None:
                    formulas.append({'셀': address, '수식': formula[:4000], '저장된 값': value[:4000]})
                    truncated |= len(formula) > 4000
                truncated |= len(value) > 4000
                values[column or '값'] = value[:4000]
            rows.append({'행': row.attrib.get('r', ''), **values})
        return {'kind': 'xlsx', 'sheets': names, 'index': index, 'rows': rows, 'formulas': formulas,
                'merged_ranges': [x.attrib.get('ref', '') for x in sheet.findall('{*}mergeCells/{*}mergeCell')[:100]],
                'truncated': truncated or len(all_rows) > 100 or any(len(r.findall('{*}c')) > 50 for r in all_rows[:100])}
    finally:
        package.zip.close()


def create_preview(raw, kind, index=0, content_type='', region=None):
    if not raw or len(raw) > MAX_INPUT: raise PreviewError('파일 크기를 확인하세요.')
    if kind == 'html': return html_preview(raw, content_type)
    if kind in ('png', 'jpg', 'jpeg', 'gif', 'webp'): return image_preview(raw)
    if kind == 'pdf': return pdf_preview(raw, index, region)
    if kind == 'docx': return word_preview(raw)
    if kind == 'xlsx': return sheet_preview(raw, index)
    if kind in ('txt', 'md', 'json', 'xml', 'csv'):
        return {'kind': 'text', 'text': decode_text(raw[:2 << 20], content_type)[:MAX_TEXT],
                'truncated': len(raw) > MAX_TEXT}
    return {'kind': 'unsupported'}


def preview(raw, kind, index=0, content_type='', region=None):
    """Run native/image/office parsing away from the Streamlit process."""
    if len(raw) > MAX_INPUT: raise PreviewError('미리보기 크기 한도를 초과합니다.')
    with tempfile.TemporaryDirectory(prefix='data-preview-') as tmp:
        source, output = Path(tmp) / 'source', Path(tmp) / 'preview.json'
        source.write_bytes(raw)
        spec = {'source': str(source), 'output': str(output), 'kind': kind, 'index': int(index), 'content_type': content_type, 'region': region}
        env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL') if k in os.environ}
        env.update(PYTHONDONTWRITEBYTECODE='1', OMP_NUM_THREADS='1')
        try:
            result = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), json.dumps(spec)],
                                    env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=10, check=False)
        except subprocess.TimeoutExpired:
            raise PreviewError('미리보기 처리 시간이 초과되었습니다. 원본을 다운로드해 확인할 수 있습니다.') from None
        if result.returncode or not output.is_file() or output.stat().st_size > MAX_OUTPUT:
            raise PreviewError('이 파일의 미리보기를 만들 수 없습니다. 원본 다운로드는 사용할 수 있습니다.')
        value = json.loads(output.read_text())
        if value.get('error'): raise PreviewError(value['error'])
        return value


def worker():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (6, 6))
    resource.setrlimit(resource.RLIMIT_AS, (768 << 20, 768 << 20))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    spec = json.loads(sys.argv[1])
    try:
        with open(spec['source'], 'rb') as source:
            raw = source.read(MAX_INPUT + 1)
        value = create_preview(raw, spec['kind'], spec['index'], spec['content_type'], spec.get('region'))
    except PreviewError as e:
        value = {'error': str(e)}
    except Exception:
        value = {'error': '파일이 손상되었거나 미리보기가 지원하지 않는 형식입니다.'}
    data = json.dumps(value, ensure_ascii=False).encode('utf-8')
    if len(data) > MAX_OUTPUT: raise SystemExit(1)
    Path(spec['output']).write_bytes(data)


if __name__ == '__main__':
    worker()
