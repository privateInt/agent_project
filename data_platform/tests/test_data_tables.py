"""Stored table review and bounded PDF crops. No inference or service mutations."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get('DATA_CONSOLE_SOURCE_DIR', Path(__file__).resolve().parents[1] / 'console'))
sys.path.insert(0, str(ROOT))
from PIL import Image
from streamlit.testing.v1 import AppTest
import data_preview
import data_results
from test_data_preview import pdf_fixture

SOURCE = '814748f8-9b33-442f-95f6-0e584760d00a'


def fixture():
    raw = pdf_fixture()
    page = data_preview.pdf_preview(raw, 0)
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(raw) as pdf:
        p = pdf[0]
        try: w, h = p.get_size()
        finally: p.close()
    location = {'page': 1, 'bbox': [10,10,w-10,h-10], 'coordinate_system': 'pdf-points-top-left'}
    candidate = {'type': 'table', 'location': location,
                 'cells': [{'row': 0, 'column': 0, 'row_span': 1, 'column_span': 1, 'text': '1200'}]}
    row = {'id': 't1', 'page': 1, 'bbox': location['bbox'], 'coordinate_system': location['coordinate_system'],
           'status': 'needs_review', 'selected': 'parser', 'reasons': ['numeric_conflict'],
           'parser_candidates': [candidate], 'ocr_candidates': [{**candidate, 'cells': [{**candidate['cells'][0], 'text': '900'}]}],
           'differences': [{'row': 0, 'column': 0, 'kind': 'text', 'parser': '1200', 'ocr': '900'}]}
    source = {'document_id': SOURCE, 'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw), 'format': 'pdf'}
    value = {'schema_version': 'canonical-document-v1', 'source': {'document_id': SOURCE, 'sha256': source['sha256'], 'format': 'pdf'},
             'blocks': [candidate], 'pages': [{'number': 1, 'width': w, 'height': h, 'rotation': 0}],
             'table_comparison': {'version': 'table-comparison-v1', 'tables': [row]},
             'quality': {'state': 'needs_review', 'issues': [{'code': 'table_comparison_requires_review', 'location': {'page': 1}}]}}
    return raw, source, value


class TableViewTests(unittest.TestCase):
    def test_downloads_keep_selected_values_and_audit_remains_only_in_json(self):
        _,_,value=fixture()
        for text in (data_results.markdown_document(value),data_results.plain_document(value)):
            self.assertIn('1200',text);self.assertNotIn('900',text);self.assertNotIn('표 비교',text)
        self.assertIn('900',json.dumps(value))
        self.assertIn('원본',data_results.quality_messages(value)[0])

    def test_processing_mode_survives_downloads_and_ocr_priority_is_not_replaced(self):
        _,_,value=fixture()
        value['processing']={'version':'refinement-processing-v1','mode':'ocr_priority'}
        value['table_comparison']['policy']='ocr-priority-v1'
        row=value['table_comparison']['tables'][0]
        row['selected']='ocr'
        value['blocks']=row['ocr_candidates']
        for text in (data_results.plain_document(value),data_results.markdown_document(value)):
            self.assertIn('OCR 우선',text)
            self.assertIn('900',text)
            self.assertNotIn('1200',text)
        value['processing']['mode']='parser_only'
        self.assertIn('OCR을 요청하지 않았습니다',data_results.markdown_document(value))

    def test_candidate_table_escapes_hostile_content(self):
        _, _, value = fixture()
        candidate = value['table_comparison']['tables'][0]['ocr_candidates'][0]
        candidate['cells'][0]['text'] = '<script>alert(1)</script><img src="http://invalid">'
        candidate['html'] = '<script>raw</script>'
        html = data_results.table_html(candidate)
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('raw', html)

    def test_crop_real_pdf_and_reject_wrong_geometry(self):
        raw, _, value = fixture()
        page = value['pages'][0]
        region = {k: page[k] for k in ('width','height','rotation')}
        region['bbox'] = [10, 10, page['width']/2, page['height']/2]
        full = data_preview.pdf_preview(raw, 0)
        cropped = data_preview.preview(raw, 'pdf', region=region)
        with Image.open(io.BytesIO(base64.b64decode(full['png']))) as f, Image.open(io.BytesIO(base64.b64decode(cropped['png']))) as c:
            self.assertLess(c.width, f.width)
            self.assertLess(c.height, f.height)
            expected = f.crop((int(10/page['width']*f.width), int(10/page['height']*f.height),
                               (f.width+1)//2, (f.height+1)//2))
            self.assertEqual(c.tobytes(), expected.tobytes())
        for changed in ({'rotation': 90}, {'width': page['width']+10}, {'bbox': [-1,0,20,20]}, {'bbox': [0,0,float('nan'),20]}):
            with self.assertRaises(data_preview.PreviewError): data_preview.pdf_preview(raw, 0, {**region, **changed})

    def test_result_screen_has_no_comparison_controls_or_hidden_requests(self):
        raw,source,value=fixture()
        code="""import streamlit as st, json
from data_ui_documents import canonical_preview
canonical_preview(json.dumps(st.session_state.value).encode(),source=st.session_state.source)
"""
        with patch('data_api.API.download') as download, patch('data_api.API.create') as submit:
            for legacy in (False,True):
                if legacy:value.pop('table_comparison',None)
                app=AppTest.from_string(code)
                app.session_state['value']=value;app.session_state['source']=source
                app.run()
                self.assertFalse(app.exception)
                self.assertFalse(app.toggle)
                for group in (app.caption,app.text,app.info):
                    self.assertFalse(any('표 비교' in x.value for x in group))
            download.assert_not_called();submit.assert_not_called()

    def test_markdown_tables_escape_pipes_and_keep_merged_values_and_visual_order(self):
        value={'blocks':[{'type':'paragraph','text':'Before'},
            {'type':'paragraph','text':'Image label','visual_region_id':'v1'},
            {'type':'table','visual_region_id':'v1','cells':[
                {'row':0,'column':0,'text':'Item','header':True},
                {'row':0,'column':1,'text':'Value','header':True},
                {'row':1,'column':0,'text':'A|B','row_span':2},
                {'row':1,'column':1,'text':'<script>1</script>\nnext'},
                {'row':2,'column':1,'text':'42'}]},
            {'type':'paragraph','text':'After'}]}
        md=data_results.markdown_document(value)
        self.assertNotIn('<table>',md);self.assertNotIn('<script>',md)
        self.assertIn('A\\|B',md);self.assertEqual(md.count('A\\|B'),2)
        self.assertIn('| Item | Value |',md);self.assertIn('42',md)
        self.assertEqual(md.count('### 그림에서 읽은 내용'),1)
        self.assertLess(md.index('Before'),md.index('Image label'))
        self.assertLess(md.index('42'),md.index('After'))
        self.assertIn('그림에서 읽은 내용',data_results.plain_document(value))
        self.assertIn('그림에서 읽은 내용',data_results.reading_html(value['blocks']))

    def test_visual_summary_keeps_unread_counts_and_old_image_text(self):
        value={'ocr':{'pages':[{'visual_regions':[{'status':'completed'},{'status':'failed'},{'status':'skipped'}]}]},
               'blocks':[{'type':'image','text':'Existing image text'}]}
        self.assertEqual(data_results.visual_summary(value),'그림 내부 글자 인식: 1개 영역 완료 · 2개 영역 미확인')
        self.assertIn('Existing image text',data_results.markdown_document(value))

    def test_partial_visual_summary_and_download_do_not_claim_completion(self):
        value = {'ocr': {'pages': [{'visual_regions': [{'status': 'partial'}, {'status': 'failed'}]}]},
                 'blocks': [{'type': 'image', 'text': '', 'recognition_status': 'unread'},
                            {'type': 'paragraph', 'text': 'Caption only', 'visual_region_id': 'v1'}]}
        summary = data_results.visual_summary(value)
        self.assertEqual(summary, '그림 내부 글자 인식: 0개 영역 완료 · 1개 영역 부분 인식 · 1개 영역 미확인')
        for render in (data_results.markdown_document, data_results.plain_document):
            text = render(value)
            self.assertIn('부분 인식', text)
            self.assertEqual(text.count('Caption only'), 1)
        self.assertEqual(data_results.reading_html(value['blocks']).count('Caption only'), 1)

if __name__ == '__main__':
    unittest.main()
