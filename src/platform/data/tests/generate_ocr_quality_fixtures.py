"""Author synthetic Korean quality fixtures; generation is not needed on servers.

Requires ReportLab, pypdf, pypdfium2 and Pillow. Bundled Nanum Gothic is OFL.
The images are rasterized from authored text; reference answers are never OCR.
"""
import copy
import hashlib
import io
import json
from importlib.metadata import version
from pathlib import Path

from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader
from pypdf import PdfReader, PdfWriter
import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parent / 'fixtures/ocr-quality'
WIDTH, HEIGHT = 595.28, 841.89


def group(texts, x, top, width, size=13, leading=27):
    return {'lines': texts, 'x': x, 'top': top, 'width': width, 'size': size,
            'leading': leading, 'bbox': [x - 4, top - 4, x + width + 4, top + leading * len(texts) + 4]}


def page(title, groups, tables=None, mode='ocr'):
    return {'title': title, 'mode': mode, 'groups': [group([title], 48, 50, 500, 22, 32), *groups], 'tables': tables or []}


def table():
    cells = [
        (0, 0, 1, 4, '2026년 9월 구매 목록'),
        (1, 0, 1, 1, '분류'), (1, 1, 1, 1, '품목'), (1, 2, 1, 1, '수량'), (1, 3, 1, 1, '금액'),
        (2, 0, 2, 1, '사무용품'), (2, 1, 1, 1, '노트'), (2, 2, 1, 1, '12'), (2, 3, 1, 1, '36,000원'),
        (3, 1, 1, 1, '연필'), (3, 2, 1, 1, '30'), (3, 3, 1, 1, '15,000원'),
        (4, 0, 1, 3, '합계'), (4, 3, 1, 1, '51,000원')]
    return {'x': [48, 148, 326, 402, 548], 'top': 130, 'row_height': 44,
            'bbox': [48, 130, 548, 350],
            'cells': [{'row': r, 'column': c, 'row_span': rs, 'column_span': cs, 'text': text}
                      for r, c, rs, cs, text in cells]}


def definitions():
    body = page('문서 처리 안내', [group([
        '접수번호: DOC-2026-0915', '담당 부서: 데이터운영팀',
        '처리 기한: 2026년 9월 30일', '결제 금액: 128,400원 (부가세 포함)',
        '문의 시간은 평일 오전 9시부터 오후 6시까지입니다.',
        '원본 문서의 이름과 접수번호를 함께 확인하세요.'], 48, 130, 500)])
    tabular = page('물품 신청 내역', [group(['납품 장소: 본관 2층 자료실', '요청 부서: 데이터운영팀'], 48, 400, 500)], [table()])
    columns = page('자료 검토 절차', [
        group(['1. 수집 단계', '가. 원본 파일을 등록합니다.', '나. 문서 형식을 확인합니다.', '다. 원본 해시를 기록합니다.'], 48, 130, 225, 11.5, 36),
        group(['2. 정제 단계', '라. 필요한 페이지를 판독합니다.', '마. 표의 셀 값을 확인합니다.', '바. 결과와 근거를 보관합니다.'], 320, 130, 228, 11.5, 36)])
    native = page('혼합 문서 - 텍스트 페이지', [group([
        '문서번호: MIX-2026-01', '이 페이지에는 검색 가능한 텍스트가 있습니다.',
        '원본 텍스트를 보존하고 다음 스캔 페이지만 판독합니다.'], 48, 130, 500)], mode='native')
    scanned = page('혼합 문서 - 스캔 페이지', [group([
        '문서번호: MIX-2026-02', '검토 항목: 한글 문장과 숫자',
        '보관 수량: 247개', '최종 확인일: 2026년 10월 5일'], 48, 130, 500)])
    body['markers'] = [body['title'], '접수번호:', '담당 부서:', '처리 기한:', '결제 금액:', '문의 시간은', '원본 문서의']
    body['critical'] = ['DOC-2026-0915', '128,400원', '2026년 9월 30일']
    tabular['markers'] = [tabular['title'], '납품 장소:', '요청 부서:']
    columns['markers'] = [columns['title'], '1. 수집 단계', '가. 원본 파일', '나. 문서 형식',
                          '다. 원본 해시', '2. 정제 단계', '라. 필요한 페이지', '마. 표의 셀', '바. 결과와 근거']
    native['markers'] = [native['title'], '문서번호:', '이 페이지에는', '원본 텍스트를']
    scanned['markers'] = [scanned['title'], '문서번호:', '검토 항목:', '보관 수량:', '최종 확인일:']
    scanned['critical'] = ['MIX-2026-02', '247개', '2026년 10월 5일']
    return [('korean-body', '한국어 본문·숫자', [body]), ('korean-table', '병합 표·셀 값', [tabular]),
            ('two-columns', '2단 읽기 순서', [columns]), ('mixed-pages', '텍스트·스캔 페이지 선택', [native, scanned])]


def authored_pdf(definition):
    stream = io.BytesIO()
    c = canvas.Canvas(stream, pagesize=(WIDTH, HEIGHT), invariant=1, pageCompression=1)
    c.setTitle(definition['title'])
    for g in definition['groups']:
        c.setFont('FixtureKorean', g['size'])
        c.setFillColorRGB(0.07, 0.09, 0.12)
        for index, text in enumerate(g['lines']):
            if pdfmetrics.stringWidth(text, 'FixtureKorean', g['size']) > g['width']:
                raise ValueError('Authored line exceeds its column: ' + text)
            c.drawString(g['x'], HEIGHT - g['top'] - g['size'] - index * g['leading'], text)
    for t in definition['tables']:
        c.setLineWidth(0.8)
        for cell in t['cells']:
            x0, x1 = t['x'][cell['column']], t['x'][cell['column'] + cell['column_span']]
            top = t['top'] + cell['row'] * t['row_height']
            height = cell['row_span'] * t['row_height']
            c.setFillColorRGB(*(0.92, 0.95, 0.98) if cell['row'] < 2 else (1, 1, 1))
            c.rect(x0, HEIGHT - top - height, x1 - x0, height, stroke=1, fill=1)
            c.setFillColorRGB(0.07, 0.09, 0.12)
            c.setFont('FixtureKorean', 12)
            c.drawCentredString((x0 + x1) / 2, HEIGHT - top - height / 2 - 4, cell['text'])
    c.showPage(); c.save()
    return stream.getvalue()


def scan_pdf(original):
    with pdfium.PdfDocument(original) as pdf:
        p = pdf[0]
        bitmap = p.render(scale=2.5)
        image = bitmap.to_pil().convert('RGB')
        try:
            stream = io.BytesIO()
            c = canvas.Canvas(stream, pagesize=(WIDTH, HEIGHT), invariant=1, pageCompression=1)
            c.drawImage(ImageReader(image), 0, 0, WIDTH, HEIGHT)
            c.showPage(); c.save()
            return stream.getvalue()
        finally:
            image.close(); bitmap.close(); p.close()


def reference(definition, number):
    texts = ['\n'.join(g['lines']) for g in definition['groups']]
    # These anchors are authored text plus source regions, independent of OCR.
    anchors = [{'text': g['lines'][0], 'bbox': g['bbox']} for g in definition['groups']]
    if definition['tables']:
        anchors.append({'text': '51,000원', 'bbox': definition['tables'][0]['bbox']})
    for text in definition.get('critical', []):
        anchors.append({'text': text, 'bbox': definition['groups'][1]['bbox']})
    return {'number': number, 'mode': definition['mode'], 'width': WIDTH, 'height': HEIGHT,
            'text': '\n'.join(texts), 'anchors': anchors, 'reading_order': definition['markers'],
            'tables': [{'bbox': t['bbox'], 'cells': copy.deepcopy(t['cells'])} for t in definition['tables']]}


def generate():
    font = ROOT / 'fonts/NanumGothic-Regular.ttf'
    pdfmetrics.registerFont(TTFont('FixtureKorean', str(font)))
    suite = {'schema_version': 1, 'suite_id': 'korean-layout-v1', 'rubric_version': 'ocr-quality-v1',
             'description': 'Authored synthetic Korean fixtures; not a production-document benchmark.',
             'font_sha256': hashlib.sha256(font.read_bytes()).hexdigest(),
             'generation_versions': {name: version(name) for name in ['reportlab', 'pypdf', 'pypdfium2', 'pillow']}, 'cases': []}
    preview = PdfWriter()
    for case_id, title, pages in definitions():
        writer = PdfWriter()
        for definition in pages:
            original = authored_pdf(definition)
            pdf = scan_pdf(original) if definition['mode'] == 'ocr' else original
            writer.add_page(PdfReader(io.BytesIO(pdf)).pages[0])
        path = ROOT / (case_id + '.pdf')
        writer.add_metadata({'/Producer': 'data-platform-quality-fixtures-v1'})
        with path.open('wb') as stream: writer.write(stream)
        for p in writer.pages: preview.add_page(p)
        suite['cases'].append({'id': case_id, 'title': title, 'file': path.name,
                               'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                               'pages': [reference(p, n + 1) for n, p in enumerate(pages)]})
    with (ROOT / 'preview.pdf').open('wb') as stream: preview.write(stream)
    (ROOT / 'suite.json').write_text(json.dumps(suite, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    generate()
