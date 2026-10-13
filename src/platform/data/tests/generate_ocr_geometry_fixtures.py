"""Author rotation/crop/JPEG fixtures. Server execution uses prebuilt assets.

Requires the same authoring packages and OFL font as the Korean quality corpus.
PDF /Rotate is countered in stored raster pixels so each displayed test page
is upright. This tests page transforms, not recognition of sideways text.
"""
import copy
import hashlib
import io
import json
from importlib.metadata import version
from pathlib import Path

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject
import pypdfium2 as pdfium
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

ROOT = Path(__file__).resolve().parent / 'fixtures/ocr-geometry'
WIDTH, HEIGHT = 595.28, 841.89
LINES = [
    ('좌표 변환 검증', [48, 55, 550, 95], 23),
    ('왼쪽 위: 101', [48, 160, 272, 190], 16),
    ('오른쪽 위: 202', [330, 160, 550, 190], 16),
    ('문서번호: GEO-2026-0915', [48, 340, 550, 370], 16),
    ('보관 수량: 247개', [48, 395, 550, 425], 16),
    ('확인일: 2026년 10월 5일', [48, 450, 550, 480], 16),
    ('왼쪽 아래: 303', [48, 680, 272, 710], 16),
    ('오른쪽 아래: 404', [330, 680, 550, 710], 16),
]


def pdf_bytes(image, width=WIDTH, height=HEIGHT):
    stream = io.BytesIO()
    c = canvas.Canvas(stream, pagesize=(width, height), invariant=1)
    c.drawImage(ImageReader(image), 0, 0, width=width, height=height)
    c.showPage(); c.save()
    return stream.getvalue()


def base_image():
    stream = io.BytesIO()
    c = canvas.Canvas(stream, pagesize=(WIDTH, HEIGHT), invariant=1)
    for text, box, size in LINES:
        c.setFont('GeometryKorean', size)
        c.drawString(box[0], HEIGHT - box[1] - size, text)
    c.showPage(); c.save()
    with pdfium.PdfDocument(stream.getvalue()) as pdf:
        p = pdf[0]; bitmap = p.render(scale=2.5)
        image = bitmap.to_pil().convert('RGB').copy()
        bitmap.close(); p.close()
    return image


def reference(width, height, mode='ocr', rotation=None):
    scale_x, scale_y = width / WIDTH, height / HEIGHT
    page = {'number': 1, 'mode': mode, 'width': width, 'height': height,
            'coordinate_system': 'pdf-points-top-left' if rotation is not None else 'image-pixels-top-left',
            'text': '\n'.join(t for t, _, _ in LINES),
            'anchors': [{'text': text, 'bbox': [round(b[0] * scale_x, 3), round(b[1] * scale_y, 3),
                                               round(b[2] * scale_x, 3), round(b[3] * scale_y, 3)]} for text, b, _ in LINES],
            'reading_order': [t for t, _, _ in LINES], 'tables': []}
    if rotation is not None: page['rotation'] = rotation
    if mode == 'guarded': page['expected_reason'] = 'pdf_coordinate_mismatch'
    return page


def cover():
    stream = io.BytesIO()
    c = canvas.Canvas(stream, pagesize=(WIDTH, HEIGHT), invariant=1)
    c.setFont('GeometryKorean', 22)
    c.drawString(48, HEIGHT - 75, 'OCR 좌표·JPEG 검증 샘플')
    descriptions = [
        '다음 페이지부터 7개 입력을 순서대로 표시합니다.',
        '1-3. PDF 회전값 90 / 180 / 270도',
        '화면에서는 정방향이 되도록 저장된 그림을 역회전했습니다.',
        '4. 잘린 PDF: 좌표 불일치 보존 검사가 예상 결과입니다.',
        '5. 원점 이동 PDF: 크기가 같아도 잘못된 좌표를 막습니다.',
        '6. 일반 JPEG: 저장된 픽셀 기준으로 인식합니다.',
        '7. EXIF 방향값 6 JPEG: 자동 회전 없이 픽셀 기준입니다.',
        '이 PDF는 미리보기입니다. 실행에는 개별 원본을 사용합니다.',
        '좌표 불일치 보존 통과는 해당 문서의 OCR 성공이 아닙니다.',
    ]
    c.setFont('GeometryKorean', 12)
    for i, text in enumerate(descriptions): c.drawString(48, HEIGHT - 140 - i * 42, text)
    c.showPage(); c.save()
    return stream.getvalue()


def generate():
    ROOT.mkdir(parents=True, exist_ok=True)
    font = ROOT.parent / 'ocr-quality/fonts/NanumGothic-Regular.ttf'
    pdfmetrics.registerFont(TTFont('GeometryKorean', str(font)))
    suite = {'schema_version': 1, 'suite_id': 'rotation-crop-jpeg-v1', 'rubric_version': 'ocr-geometry-v1',
             'description': '5 OCR quality cases and 2 expected coordinate-guard cases. Synthetic, not a production benchmark.',
             'font_sha256': hashlib.sha256(font.read_bytes()).hexdigest(),
             'generation_versions': {name: version(name) for name in ('reportlab', 'pypdf', 'pypdfium2', 'pillow')}, 'cases': []}
    preview = PdfWriter(); preview.add_page(PdfReader(io.BytesIO(cover())).pages[0])
    with base_image() as source:
        source.save(ROOT / 'reference.png')
        for angle in (90, 180, 270):
            with source.rotate(angle, expand=True) as stored:
                w, h = (HEIGHT, WIDTH) if angle in (90, 270) else (WIDTH, HEIGHT)
                p = PdfReader(io.BytesIO(pdf_bytes(stored, w, h))).pages[0]
                p.rotate(angle)
                writer = PdfWriter(); writer.add_page(p)
                path = ROOT / ('rotate-' + str(angle) + '.pdf')
                with path.open('wb') as stream: writer.write(stream)
                preview.add_page(p)
                suite['cases'].append({'id': path.stem, 'title': 'PDF Rotate ' + str(angle), 'file': path.name,
                                       'pages': [reference(WIDTH, HEIGHT, rotation=angle)]})
        for name, box in [('cropped', [24, 30, WIDTH - 24, HEIGHT - 30]),
                          ('shifted-origin', [20, 30, WIDTH + 20, HEIGHT + 30])]:
            p = PdfReader(io.BytesIO(pdf_bytes(source))).pages[0]
            if name == 'cropped': p.cropbox = RectangleObject(box)
            else: p.mediabox = RectangleObject(box)
            writer = PdfWriter(); writer.add_page(p)
            path = ROOT / (name + '.pdf')
            with path.open('wb') as stream: writer.write(stream)
            preview.add_page(p)
            suite['cases'].append({'id': name, 'title': name + ' expected coordinate guard', 'file': path.name,
                                   'pages': [reference(WIDTH, HEIGHT, 'guarded', 0)]})
        for name, orientation in [('jpeg', 1), ('jpeg-exif-6', 6)]:
            exif = Image.Exif(); exif[274] = orientation
            path = ROOT / (name + '.jpg')
            source.save(path, 'JPEG', quality=92, subsampling=0, exif=exif)
            with Image.open(path) as decoded:
                preview.add_page(PdfReader(io.BytesIO(pdf_bytes(decoded))).pages[0])
            suite['cases'].append({'id': name, 'title': name + ' raw-pixel coordinates', 'file': path.name,
                                   'source_exif_orientation': orientation,
                                   'pages': [reference(source.width, source.height)]})
    for case in suite['cases']:
        case['sha256'] = hashlib.sha256((ROOT / case['file']).read_bytes()).hexdigest()
    with (ROOT / 'preview.pdf').open('wb') as stream: preview.write(stream)
    (ROOT / 'suite.json').write_text(json.dumps(suite, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    generate()
