import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
import zipfile
from live_upload_smoke import office_fixture
spec = importlib.util.spec_from_file_location('refinement_parser', Path(__file__).resolve().parents[1] / 'jobs/refinement/parse/parser.py')
parser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser)

def extract(data, kind, **overrides):
    request = {'document_id': '11111111-1111-4111-8111-111111111111', 'sha256': hashlib.sha256(data).hexdigest(), 'format': kind, 'rules_version': parser.RULES, 'image_id': 'sha256:' + 'a' * 64}
    request.update(overrides)
    return json.loads(parser.parse(data, request))

def office(kind, changes):
    with zipfile.ZipFile(io.BytesIO(office_fixture(kind))) as source:
        parts = {name: source.read(name) for name in source.namelist()}
    parts.update(changes)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items(): z.writestr(name, data)
    return out.getvalue()

def pdf_fixture(text=True, table=False, *, content=None):
    stream = content if content is not None else (b'BT /F1 12 Tf 40 160 Td (CPU text extraction) Tj ET\n' if text else b'')
    if table:
        stream += b'40 50 m 180 50 l S 40 90 m 180 90 l S 40 130 m 180 130 l S\n40 50 m 40 130 l S 110 50 m 110 130 l S 180 50 m 180 130 l S\n'
        stream += b'BT /F1 10 Tf 50 110 Td (A1) Tj 70 0 Td (B1) Tj ET\nBT /F1 10 Tf 50 70 Td (A2) Tj 70 0 Td (B2) Tj ET\n'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>', b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 220 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>', b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>', b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'endstream']
    data, offsets = b'%PDF-1.4\n', [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data)); data += str(index).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
    xref = len(data)
    data += b'xref\n0 6\n0000000000 65535 f \n' + b''.join(('%010d 00000 n \n' % n).encode() for n in offsets[1:])
    return data + b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n' + str(xref).encode() + b'\n%%EOF\n'


def borderless_fixture(rows=6, prose=False):
    stream = []
    for index in range(rows):
        y = 170 - index * 20
        cells = [(20, 'Water'), (120, str(10 + index)), (175, 'kg')]
        if prose:
            cells = [(20, 'Left %s text' % index), (125, 'Right %s text' % index)]
        for x, text in cells:
            stream.append(f'BT /F1 8 Tf {x} {y} Td ({text}) Tj ET\n')
    return pdf_fixture(content=''.join(stream).encode())

class ParserTests(unittest.TestCase):
    def test_text_preserves_indentation_and_unicode(self):
        text = 'def run():\r\n\treturn "e\u0301"  \r\n'
        result = extract(text.encode(), 'text')
        self.assertEqual(result['blocks'][0]['text'], text.replace('\r\n', '\n'))
        self.assertEqual(result['quality']['state'], 'ready')
        self.assertEqual(result['cleaning']['changed_fields'], 1)

    def test_markdown_keeps_fences_and_hard_breaks(self):
        text = '# Title\n```python\n    x = 1\n```\nline  \nnext\n'
        self.assertEqual(extract(text.encode(), 'markdown')['blocks'][0]['text'], text)

    def test_provenance_and_determinism(self):
        a = extract(b'input', 'text')
        self.assertEqual(a, extract(b'input', 'text'))
        self.assertEqual(a['source']['sha256'], hashlib.sha256(b'input').hexdigest())
        self.assertEqual(a['processor']['rules_version'], parser.RULES)

    def test_optional_timing_keeps_content_and_does_not_change_default_output(self):
        plain = extract(b'input', 'text')
        timed = extract(b'input', 'text', measure_timings=True)
        measured = timed.pop('timings')
        self.assertEqual(timed, plain)
        self.assertEqual(measured['version'], 'refinement-timing-v1')
        self.assertEqual(measured['ocr_ms'], 0)
        self.assertGreaterEqual(measured['parser_total_ms'], measured['native_parse_ms'])
        self.assertNotIn('timings', plain)

    def test_wrong_hash_or_unknown_rules_rejected(self):
        for values in [{'sha256': 'b' * 64}, {'rules_version': 'unknown'}]:
            with self.assertRaises(parser.ParseError): extract(b'input', 'text', **values)

    def test_invalid_sources_rejected(self):
        for data, kind in [(b'\xff', 'text'), (b'broken', 'png'), (b'broken', 'pdf'), (b'broken', 'docx'), (b'text', 'unknown')]:
            with self.subTest(kind=kind), self.assertRaises(Exception): extract(data, kind)

    def test_html_drops_scripts_preserves_tail_and_code(self):
        value = extract(b'<html><body><p>before<script>SECRET</script>after</p><pre>if x:\n    run()</pre></body></html>', 'html')
        self.assertEqual([b['text'] for b in value['blocks']], ['beforeafter', 'if x:\n    run()'])
        self.assertNotIn('SECRET', json.dumps(value))

    def test_html_spans_and_image_state(self):
        value = extract(b'<table><tr><th rowspan="2">A</th><td>B</td></tr><tr><td>C</td></tr></table><img src="https://example.invalid/a.png" alt="chart">', 'html')
        table = next(b for b in value['blocks'] if b['type'] == 'table')
        self.assertEqual(table['cells'][0]['row_span'], 2)
        self.assertEqual(table['cells'][2]['column'], 1)
        self.assertEqual(value['quality']['state'], 'needs_ocr')

    def test_html_excessive_grid_rejected(self):
        with self.assertRaises(parser.ParseError): extract(b'<table><tr><td rowspan="1000" colspan="1000">x</td></tr></table>', 'html')

    def test_docx_order_and_merges(self):
        body = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>before</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>merged</w:t></w:r></w:p></w:tc></w:tr><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge/></w:tcPr><w:p/></w:tc></w:tr></w:tbl><w:p><w:r><w:t>after</w:t></w:r></w:p></w:body></w:document>'
        value = extract(office('docx', {'word/document.xml': body}), 'docx')
        self.assertEqual([b['type'] for b in value['blocks']], ['paragraph', 'table', 'paragraph'])
        cell = value['blocks'][1]['cells'][0]
        self.assertEqual((cell['row_span'], cell['column_span'], cell['text']), (2, 2, 'merged'))
        self.assertEqual(value['quality']['state'], 'needs_review')

    def test_docx_visual_not_silent_success(self):
        body = '<w:document xmlns:w="urn:test"><w:body><w:p><w:r><w:drawing/></w:r></w:p></w:body></w:document>'
        self.assertEqual(extract(office('docx', {'word/document.xml': body}), 'docx')['quality']['state'], 'needs_ocr')

    def test_utf16_dtd_rejected(self):
        raw = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY e "hidden">]><x>&e;</x>'.encode('utf-16')
        with self.assertRaises(parser.ParseError): extract(office('docx', {'word/document.xml': raw}), 'docx')

    def test_zip_traversal_rejected(self):
        with self.assertRaises(parser.ParseError): extract(office('docx', {'../outside': 'bad'}), 'docx')

    def test_xlsx_sparse_cells_formulas_and_cache(self):
        sheet = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>  code  </t></is></c><c r="C1"><v>10.00</v></c><c r="D1"><f>C1*2</f><v>20</v></c></row><row r="1000000"><c r="XFD1000000" t="inlineStr"><is><t>far</t></is></c></row></sheetData><mergeCells><mergeCell ref="A1:B1"/></mergeCells></worksheet>'
        table = extract(office('xlsx', {'xl/worksheets/sheet1.xml': sheet}), 'xlsx')['blocks'][0]
        self.assertEqual([c['address'] for c in table['cells']], ['A1', 'C1', 'D1', 'XFD1000000'])
        self.assertEqual(table['cells'][0]['value'], '  code  ')
        self.assertEqual(table['cells'][1]['value'], '10.00')
        self.assertEqual(table['cells'][2]['formula'], 'C1*2')
        self.assertEqual(table['cells'][2]['cached_value'], '20')
        self.assertEqual(table['merged_ranges'], ['A1:B1'])
        self.assertEqual(table['cells'][3]['column'], 16383)

    def test_xlsx_external_sheet_not_fetched(self):
        rels = '<Relationships><Relationship Id="rId1" Type="urn:worksheet" Target="https://example.invalid/sheet" TargetMode="External"/></Relationships>'
        with self.assertRaises(parser.ParseError): extract(office('xlsx', {'xl/_rels/workbook.xml.rels': rels}), 'xlsx')

    def test_pdf_coordinates_and_read_order_warning(self):
        value = extract(pdf_fixture(), 'pdf')
        text = next(b for b in value['blocks'] if b['type'] == 'text')
        self.assertIn('CPU text extraction', text['text'])
        self.assertEqual(text['location']['page'], 1)
        self.assertEqual(len(text['location']['bbox']), 4)
        self.assertEqual(value['quality']['state'], 'needs_review')

    def test_borderless_numeric_table_requests_ocr_without_inventing_cells(self):
        value = extract(borderless_fixture(), 'pdf')
        self.assertEqual(value['quality']['state'], 'needs_ocr')
        self.assertTrue(any(i['code'] == 'pdf_borderless_table_requires_ocr_review' and i['requires_ocr']
                            for i in value['quality']['issues']))
        self.assertFalse(any(b['type'] == 'table' for b in value['blocks']))
        self.assertIn('15', '\n'.join(b.get('text', '') for b in value['blocks']))

    def test_two_column_prose_and_sparse_numeric_lines_do_not_trigger_table_ocr(self):
        for raw in (borderless_fixture(prose=True), borderless_fixture(rows=3), pdf_fixture()):
            with self.subTest(size=len(raw)):
                value = extract(raw, 'pdf')
                self.assertFalse(any(i['requires_ocr'] for i in value['quality']['issues']))

    def test_pdf_table_not_duplicated(self):
        value = extract(pdf_fixture(table=True), 'pdf')
        tables = [b for b in value['blocks'] if b['type'] == 'table']
        self.assertEqual(len(tables), 1)
        self.assertEqual([c['text'] for c in tables[0]['cells']], ['A1', 'B1', 'A2', 'B2'])
        self.assertNotIn('A1', ' '.join(b.get('text', '') for b in value['blocks']))

    def test_blank_pdf_needs_ocr(self):
        value = extract(pdf_fixture(text=False), 'pdf')
        self.assertEqual(value['quality']['state'], 'needs_ocr')
        self.assertEqual(value['pages'][0]['coordinate_system'], 'pdf-points-top-left')

    def test_image_dimensions_without_fake_text(self):
        from PIL import Image
        stream = io.BytesIO(); Image.new('RGB', (15, 21)).save(stream, format='PNG')
        value = extract(stream.getvalue(), 'png')
        self.assertEqual(value['quality']['state'], 'needs_ocr')
        self.assertEqual(value['blocks'][0]['location']['bbox'], [0, 0, 15, 21])
        self.assertNotIn('text', value['blocks'][0])

class ParserCLITests(unittest.TestCase):
    def invoke(self, directory, data, spec, output_exists=False):
        root = Path(directory)
        (root / 'source').write_bytes(data)
        (root / 'spec.json').write_text(json.dumps(spec), encoding='utf-8')
        if output_exists:
            (root / 'output.json').write_text('retained-existing-output')
        return subprocess.run([sys.executable, '-I', '-B', '-u', str(Path(parser.__file__).resolve()),
            '--input', str(root / 'source'), '--spec', str(root / 'spec.json'), '--output', str(root / 'output.json')],
            env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'}, capture_output=True, text=True, timeout=20)

    def test_real_isolated_cli_writes_unicode_text(self):
        data = '원본 문서\r\n  공백과 줄바꿈을 그대로 보존합니다.\t123\n'.encode()
        spec = {'document_id': '11111111-1111-4111-8111-111111111111', 'sha256': hashlib.sha256(data).hexdigest(),
                'format': 'text', 'rules_version': parser.RULES, 'image_id': 'sha256:' + 'a' * 64}
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(directory, data, spec)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads((Path(directory) / 'output.json').read_text())
            self.assertEqual(output['blocks'][0]['text'], data.decode().replace('\r\n', '\n'))
            self.assertEqual(result.stdout, '')

    def test_cli_failure_categories_do_not_expose_source(self):
        secret = b'private source content'
        spec = {'document_id': '11111111-1111-4111-8111-111111111111', 'sha256': hashlib.sha256(secret).hexdigest(),
                'format': 'text', 'rules_version': parser.RULES, 'image_id': 'sha256:' + 'a' * 64}
        for expected, values, exists in [('invalid_spec', {}, False), ('output_exists', spec, True)]:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                result = self.invoke(directory, secret, values, output_exists=exists)
                self.assertEqual(result.returncode, 1)
                prefix = 'DATA_PARSER_DIAGNOSTIC_V1 '
                self.assertTrue(result.stderr.startswith(prefix))
                diagnostic = json.loads(result.stderr[len(prefix):])
                self.assertEqual(diagnostic['reason'], expected)
                self.assertNotIn(secret.decode(), result.stderr)
                self.assertNotIn(directory, result.stderr)
                if exists:
                    self.assertEqual((Path(directory) / 'output.json').read_text(), 'retained-existing-output')


if __name__ == '__main__': unittest.main()
