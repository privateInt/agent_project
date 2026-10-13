"""Real preview workers and hostile fixtures; no network or Jobs required."""
import base64
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(os.environ.get('DATA_CONSOLE_SOURCE_DIR', Path(__file__).resolve().parents[1] / 'console'))
sys.path.insert(0, str(ROOT))
from data_preview import preview, PreviewError, html_preview
from data_state import download_name, source_kind, source_type_label
from PIL import Image


def package(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items(): archive.writestr(name, value)
    return stream.getvalue()


def pdf_fixture():
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Resources << >> /Contents 4 0 R >>',
        b'<< /Length 25 >>\nstream\n1 0 0 rg 0 0 200 100 re f\n\nendstream',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Resources << >> /Contents 6 0 R >>',
        b'<< /Length 25 >>\nstream\n0 0 1 rg 0 0 200 100 re f\n\nendstream']
    raw, offsets = b'%PDF-1.4\n', [0]
    for n, obj in enumerate(objects, 1):
        offsets.append(len(raw))
        raw += str(n).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
    start = len(raw)
    raw += b'xref\n0 7\n0000000000 65535 f \n'
    raw += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    return raw + f'trailer\n<< /Size 7 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode()


def workbook(external=False):
    return package({
        'xl/workbook.xml': '<workbook xmlns="urn:sheet" xmlns:r="urn:rel"><sheets><sheet name="매출" r:id="r1"/><sheet name="요약" r:id="r2"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml" ' + ('TargetMode="External" ' if external else '') + '/><Relationship Id="r2" Target="/xl/worksheets/sheet2.xml"/></Relationships>',
        'xl/sharedStrings.xml': '<sst xmlns="urn:sheet"><si><r><t>한글</t></r><r><t> 제목</t></r></si></sst>',
        'xl/worksheets/sheet1.xml': '<worksheet xmlns="urn:sheet"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><f>1+2</f><v>3</v></c></row></sheetData><mergeCells><mergeCell ref="A2:B2"/></mergeCells></worksheet>',
        'xl/worksheets/sheet2.xml': '<worksheet xmlns="urn:sheet"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>둘째 시트</t></is></c></row></sheetData></worksheet>'})


class PreviewTests(unittest.TestCase):
    def test_html_retains_title_body_table_but_no_active_content_or_attributes(self):
        raw = '''<html><head><title>수집 결과</title><style>HIDDENSTYLE</style></head><body>
        <h1 onclick="evil()">제목</h1><p>본문 &lt;script&gt;</p><table style="bad"><tr><td>한글</td></tr></table>
        <script>HIDDENSCRIPT</script><iframe src="http://remote">HIDDENFRAME</iframe>
        <svg><text>HIDDENSVG</text></svg><template>HIDDENTEMPLATE</template>
        <a href="javascript:evil()">연결 제목</a><img src="http://remote/x" onerror="bad" alt="설명 &lt;img&gt;">
        <form><input value="secret">HIDDENFORM</form><p>끝</p></body></html>'''.encode()
        result = preview(raw, 'html')
        self.assertEqual(result['title'], '수집 결과')
        self.assertIn('<h1>제목</h1>', result['html'])
        self.assertIn('<table><tr><td>한글</td></tr></table>', result['html'])
        self.assertIn('&lt;script&gt;', result['html'])
        self.assertIn('설명 &lt;img&gt;', result['html'])
        for forbidden in ['HIDDEN', 'href=', 'src=', 'onclick', 'style=', '<script', '<img', 'http://', '<form']:
            self.assertNotIn(forbidden, result['html'])
        self.assertIn('<p>끝</p>', result['html'])

    def test_malformed_html_and_oversize_text_stay_bounded(self):
        result = html_preview(('<h2>제목<p>' + '가' * 250000).encode())
        self.assertTrue(result['truncated'])
        self.assertLess(len(result['html']), 201000)
        self.assertTrue(result['html'].endswith('</p></h2>'))

    def test_korean_charset_and_utf16_text(self):
        self.assertIn('한글 본문', preview('<p>한글 본문</p>'.encode('cp949'), 'html', content_type='text/html; charset=euc-kr')['html'])
        self.assertEqual(preview('한글 본문'.encode('utf-16'), 'txt')['text'], '한글 본문')

    def test_pdf_rasterizes_selected_page_and_preserves_source(self):
        raw = pdf_fixture()
        results = [preview(raw, 'pdf', i) for i in (0, 1)]
        self.assertEqual(results[0]['page_count'], 2)
        colors = []
        for result in results:
            with Image.open(io.BytesIO(base64.b64decode(result['png']))) as image:
                self.assertLessEqual(max(image.size), 1200)
                colors.append(image.convert('RGB').getpixel((40, 40)))
        self.assertEqual(colors, [(255, 0, 0), (0, 0, 255)])
        self.assertEqual(raw, pdf_fixture())
        with self.assertRaises(PreviewError): preview(raw, 'pdf', 2)

    def test_image_worker_scales_actual_pixels(self):
        stream = io.BytesIO()
        Image.new('RGB', (1600, 800), '#123456').save(stream, 'PNG')
        result = preview(stream.getvalue(), 'png')
        self.assertEqual((result['width'], result['height']), (1600, 800))
        with Image.open(io.BytesIO(base64.b64decode(result['png']))) as image:
            self.assertEqual(image.size, (1200, 600))
            self.assertEqual(image.getpixel((0, 0)), (18, 52, 86))

    def test_word_extracts_namespaced_runs_and_tables_as_escaped_text(self):
        raw = package({'word/document.xml': '<w:document xmlns:w="urn:word"><w:body><w:p><w:r><w:t>한글 &lt;script&gt;</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>표 값</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'})
        result = preview(raw, 'docx')
        self.assertIn('<p>한글 &lt;script&gt;</p>', result['html'])
        self.assertIn('<td>표 값</td>', result['html'])

    def test_spreadsheet_selects_sheets_and_shows_cached_values_without_evaluation(self):
        first = preview(workbook(), 'xlsx')
        self.assertEqual(first['sheets'], ['매출', '요약'])
        self.assertEqual(first['rows'], [{'행': '1', 'A': '한글 제목', 'B': '3'}])
        self.assertEqual(first['formulas'], [{'셀': 'B1', '수식': '1+2', '저장된 값': '3'}])
        self.assertEqual(first['merged_ranges'], ['A2:B2'])
        second = preview(workbook(), 'xlsx', 1)
        self.assertEqual(second['rows'][0]['A'], '둘째 시트')
        with self.assertRaises(PreviewError): preview(workbook(True), 'xlsx')

    def test_office_entities_and_zip_bombs_rejected(self):
        xml = '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><document><body><p>&e;</p></body></document>'
        for encoded in [xml.encode(), xml.encode('utf-16')]:
            with self.assertRaises(PreviewError): preview(package({'word/document.xml': encoded}), 'docx')
        large = package({'word/document.xml': b'x' * ((8 << 20) + 1)})
        with self.assertRaises(PreviewError): preview(large, 'docx')

    def test_corrupt_and_unsupported_files_have_download_fallback(self):
        for kind in ('pdf', 'png', 'docx', 'xlsx'):
            with self.subTest(kind=kind), self.assertRaises(PreviewError): preview(b'broken', kind)
        self.assertEqual(preview(b'unknown', 'bin'), {'kind': 'unsupported'})

    def test_timeout_does_not_hang_ui(self):
        with patch('data_preview.subprocess.run', side_effect=subprocess.TimeoutExpired('worker', 10)):
            with self.assertRaisesRegex(PreviewError, '시간'): preview(b'hello', 'txt')

    def test_extension_and_format_labels_follow_metadata(self):
        for mime, kind in [('application/pdf; qs=0.001', 'pdf'), ('text/html; charset=utf-8', 'html'),
                           ('image/png', 'png'), ('application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'docx')]:
            meta = {'content_type': mime}
            self.assertEqual(source_kind(meta), kind)
            self.assertTrue(download_name('source-documents', '814748f8-9b33-442f-95f6-0e584760d00a', meta).endswith('.' + kind))
        self.assertIn('Word', source_type_label({'format': 'docx'}))
        self.assertTrue(download_name('source-documents', '814748f8-9b33-442f-95f6-0e584760d00a', {'format': 'xlsx', 'original_filename': '매출.xlsx'}).endswith('매출.xlsx'))


if __name__ == '__main__':
    unittest.main()
