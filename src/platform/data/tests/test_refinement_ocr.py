import copy
import hashlib
import http.client
import http.server
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from PIL import Image
from test_refinement_parser import parser, extract, pdf_fixture, borderless_fixture

module = importlib.util.spec_from_file_location('refinement_ocr', Path(parser.__file__).with_name('ocr.py'))
ocr = importlib.util.module_from_spec(module)
module.loader.exec_module(ocr)


def identity():
    dep = '11111111-1111-4111-8111-111111111111'
    return {'deployment_id': dep, 'model_version_id': '22222222-2222-4222-8222-222222222222',
            'runtime_image_id': 'sha256:' + 'a' * 64, 'runtime_container_id': 'b' * 64,
            'internal_endpoint': 'http://model-serving-' + dep + ':8000/v1',
            'served_model_name': 'deployment-' + dep, 'profile': ocr.PROFILE,
            'source_manifest_sha256': 'c' * 64}


def png():
    output = io.BytesIO()
    Image.new('RGB', (100, 200), 'white').save(output, 'PNG')
    return output.getvalue()


def response(text=None, **changes):
    body = {'model': identity()['served_model_name'], 'choices': [{'message': {'role': 'assistant',
            'content': text or '<div data-label="Text" data-bbox="100 200 900 800"><p>OCR example</p></div>'},
            'finish_reason': 'stop', 'logprobs': {'content': [{'logprob': -0.1}]}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}}
    body.update(changes)
    return body


def document(data=None, kind='png'):
    data = png() if data is None else data
    doc = parser.Document({'document_id': identity()['deployment_id'], 'sha256': hashlib.sha256(data).hexdigest(),
                           'format': kind, 'image_id': 'sha256:' + 'd' * 64})
    doc.value = extract(data, kind)
    return doc, data


def answer(image, pinned, deadline):
    return ocr.decode_response(json.dumps(response()).encode(), pinned['served_model_name'])


class OCRTests(unittest.TestCase):
    def test_empty_visual_keeps_readable_text_as_partial_with_coordinates(self):
        raw = ('<div data-label="Text" data-bbox="10 10 900 200">Read body</div>'
               '<div data-label="Diagram" data-bbox="100 300 800 700"><img/></div>')
        doc, data = document()
        native = copy.deepcopy(doc.value['blocks'])
        ocr.enrich(doc, data, identity(), infer_page=lambda *a: ocr.decode_response(
            json.dumps(response(raw)).encode(), identity()['served_model_name']), measure_timings=True)
        record = doc.value['ocr']['pages'][0]
        self.assertEqual((record['status'], record['reason'], record['unresolved_visual_blocks']),
                         ('partial', 'visual_content_unread', 1))
        self.assertEqual(record['native_blocks'], native)
        self.assertEqual(doc.value['blocks'][0]['text'], 'Read body')
        visual = doc.value['blocks'][1]
        self.assertEqual(visual['recognition_status'], 'unread')
        self.assertEqual(visual['location']['bbox'], [10, 60, 80, 140])
        self.assertEqual(doc.value['quality']['state'], 'needs_ocr')
        self.assertTrue(any(i['code'] == 'ocr_visual_content_unread' and i['requires_ocr']
                            and i['location']['bbox'] == visual['location']['bbox']
                            for i in doc.value['quality']['issues']))
        self.assertEqual(set(record['timings']), {'render_ms', 'request_ms', 'response_parse_ms', 'visual_regions_ms', 'total_ms'})
        self.assertTrue(all(x >= 0 for x in record['timings'].values()))

    def test_visual_only_and_empty_text_are_not_false_partial_successes(self):
        for raw, reason in (
            ('<div data-label="Image" data-bbox="0 0 1000 1000"><img/></div>', 'no_readable_blocks'),
            ('<div data-label="Text" data-bbox="0 0 1000 1000"></div>', 'empty_block_text')):
            with self.subTest(reason=reason):
                doc, data = document()
                native = copy.deepcopy(doc.value['blocks'])
                ocr.enrich(doc, data, identity(), infer_page=lambda *a: ocr.decode_response(
                    json.dumps(response(raw)).encode(), identity()['served_model_name']), measure_timings=True)
                record = doc.value['ocr']['pages'][0]
                self.assertEqual((record['status'], record['reason']), ('failed', reason))
                self.assertEqual(doc.value['blocks'], native)
                self.assertIn('response_parse_ms', record['timings'])

    def test_timings_capture_failed_request_without_inventing_unrun_stages(self):
        doc, data = document()
        call = unittest.mock.Mock(side_effect=ocr.OCRFailure('timeout'))
        with patch.object(ocr.time, 'monotonic', side_effect=range(100, 200)):
            ocr.enrich(doc, data, identity(), infer_page=call, measure_timings=True)
        timing = doc.value['ocr']['pages'][0]['timings']
        self.assertGreater(timing['request_ms'], 0)
        self.assertNotIn('response_parse_ms', timing)
        self.assertGreaterEqual(timing['total_ms'], timing['render_ms'] + timing['request_ms'])

    def test_visual_placeholder_does_not_bypass_unsafe_html_or_low_confidence(self):
        raw = ('<div data-label="Text" data-bbox="0 0 1000 200">body</div>'
               '<div data-label="Image" data-bbox="0 300 1000 900"><script>bad</script></div>')
        doc, data = document()
        ocr.enrich(doc, data, identity(), infer_page=lambda *a: ocr.decode_response(
            json.dumps(response(raw)).encode(), identity()['served_model_name']))
        self.assertEqual(doc.value['ocr']['pages'][0]['reason'], 'unsafe_html')
        self.assertEqual(doc.value['ocr']['pages'][0]['status'], 'failed')
        result = ocr.decode_response(json.dumps(response(raw.replace('<script>bad</script>', '<img/>'))).encode(), identity()['served_model_name'])
        result['page_mean_token_probability'] = 0.2
        doc, data = document()
        ocr.enrich(doc, data, identity(), infer_page=lambda *a: result)
        self.assertEqual(doc.value['ocr']['pages'][0]['reason'], 'low_token_probability')
        self.assertEqual(doc.value['ocr']['pages'][0]['status'], 'failed')

    def parse_with_inference(self, data, call):
        def enrich(doc, original, pinned, measure_timings=False):
            self.assertEqual(original, data)
            ocr.enrich(doc, original, pinned, infer_page=call, measure_timings=measure_timings)
        import runpy
        original_loader = runpy.run_path
        with patch('runpy.run_path', side_effect=lambda path: {'enrich': enrich} if Path(path).name == 'ocr.py' else original_loader(path)):
            return extract(data, 'pdf', ocr=identity())

    def test_unmodified_incomplete_pdf_reaches_real_renderer_and_inference(self):
        original = b'\r\n' * 4 + pdf_fixture(text=False).split(b'%%EOF')[0]
        before = hashlib.sha256(original).hexdigest()
        calls = []
        def infer(image, pinned, deadline):
            calls.append(image.size)
            return answer(image, pinned, deadline)
        value = self.parse_with_inference(original, infer)
        self.assertEqual(len(calls), 1)
        self.assertEqual(value['ocr']['pages'][0]['status'], 'completed')
        self.assertEqual(value['source']['sha256'], before)
        self.assertEqual(hashlib.sha256(original).hexdigest(), before)
        self.assertEqual(value['quality']['state'], 'needs_review')

    def test_native_open_failure_uses_pdfium_then_inference(self):
        calls = []
        def infer(image, pinned, deadline):
            calls.append(image.size)
            return answer(image, pinned, deadline)
        with patch('pdfplumber.open', side_effect=ValueError('native parser failure')):
            value = self.parse_with_inference(pdf_fixture(), infer)
        self.assertEqual(len(calls), 1)
        record = value['ocr']['pages'][0]
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(record['native_blocks'], [])
        self.assertIn('pdf_native_parse_failed_requires_ocr', [x['code'] for x in record['native_issues']])
        self.assertEqual(value['quality']['state'], 'needs_review')

    def test_page_failure_preserves_completed_native_page(self):
        import pdfplumber
        path = Path(__file__).parent / 'fixtures/ocr-quality/mixed-pages.pdf'
        original = path.read_bytes()
        native = extract(original, 'pdf')
        read_lines = pdfplumber.page.Page.extract_text_lines
        def damaged(page, *args, **kwargs):
            if page.page_number == 2: raise ValueError('second page extraction failed')
            return read_lines(page, *args, **kwargs)
        calls = []
        def infer(image, pinned, deadline):
            calls.append(image.size)
            return answer(image, pinned, deadline)
        with patch.object(pdfplumber.page.Page, 'extract_text_lines', damaged):
            value = self.parse_with_inference(original, infer)
        first = lambda doc: [b for b in doc['blocks'] if b['location']['page'] == 1]
        self.assertEqual(first(value), first(native))
        self.assertEqual([p['page'] for p in value['ocr']['pages']], [2])
        self.assertEqual(value['ocr']['pages'][0]['status'], 'completed')
        self.assertEqual(len(calls), 1)

    def test_fallback_without_deployment_keeps_needs_ocr(self):
        with patch('pdfplumber.open', side_effect=ValueError('native parser failure')):
            value = extract(pdf_fixture(), 'pdf')
        self.assertEqual(value['quality']['state'], 'needs_ocr')
        self.assertEqual(len(value['pages']), 1)
        self.assertNotIn('ocr', value)
        self.assertEqual(value['blocks'], [])

    def test_unrenderable_pdf_fails_without_inference(self):
        call = unittest.mock.Mock(side_effect=AssertionError('cannot infer without page image'))
        with self.assertRaises(Exception):
            self.parse_with_inference(b'%PDF-1.7\nnot a page', call)
        call.assert_not_called()

    def test_parser_resource_limits_do_not_trigger_fallback(self):
        for error in [parser.ParseError('PDF page limit exceeded'), MemoryError(), TimeoutError(), ImportError()]:
            with self.subTest(error=type(error).__name__), patch.object(parser, 'pdf_native', side_effect=error), patch('pypdfium2.PdfDocument') as renderer:
                with self.assertRaises(type(error)): extract(pdf_fixture(), 'pdf')
                renderer.assert_not_called()

    def test_fallback_page_enumeration_retains_page_limit(self):
        with patch('pdfplumber.open', side_effect=ValueError('bad native PDF')), patch.object(parser, 'MAX_PAGES', 0):
            with self.assertRaises(parser.ParseError): extract(pdf_fixture(), 'pdf')

    def test_native_failure_does_not_bypass_crop_coordinate_guard(self):
        original = (Path(__file__).parent / 'fixtures/ocr-geometry/cropped.pdf').read_bytes()
        call = unittest.mock.Mock(side_effect=AssertionError('coordinate guard'))
        with patch('pdfplumber.open', side_effect=ValueError('native PDF failed')):
            value = self.parse_with_inference(original, call)
        call.assert_not_called()
        self.assertEqual(value['quality']['state'], 'needs_ocr')
        self.assertEqual(value['ocr']['pages'][0]['reason'], 'pdf_coordinate_mismatch')

    def test_pinned_endpoint_rejects_external_destination_before_request(self):
        bad = identity()
        bad['internal_endpoint'] = 'http://example.invalid/v1'
        with patch.object(ocr.http.client, 'HTTPConnection') as request:
            with self.assertRaises(ValueError): ocr.infer(Image.new('RGB', (1, 1)), bad, 1)
            request.assert_not_called()

    def test_truncated_wrong_model_and_nonfinite_response_rejected(self):
        for kind in ('truncated', 'wrong_model', 'nan', 'empty'):
            body = response()
            if kind == 'truncated': body['choices'][0]['finish_reason'] = 'length'
            if kind == 'wrong_model': body['model'] = 'other-model'
            if kind == 'nan': body['choices'][0]['logprobs']['content'][0]['logprob'] = float('nan')
            if kind == 'empty': body['choices'][0]['message']['content'] = ''
            with self.subTest(kind=kind), self.assertRaises(ocr.OCRFailure):
                ocr.decode_response(json.dumps(body).encode(), identity()['served_model_name'])

    def test_duplicate_response_keys_rejected(self):
        with self.assertRaises(ocr.OCRFailure):
            ocr.decode_response(b'{"model":"a","model":"b"}', 'a')

    def test_success_preserves_native_result_and_maps_pixel_boxes(self):
        doc, data = document()
        native = copy.deepcopy(doc.value['blocks'])
        ocr.enrich(doc, data, identity(), infer_page=answer)
        self.assertEqual(doc.value['quality']['state'], 'needs_review')
        self.assertEqual(doc.value['ocr']['identity'], identity())
        self.assertEqual(doc.value['ocr']['pages'][0]['native_blocks'], native)
        self.assertEqual(doc.value['blocks'][0]['location']['bbox'], [10, 40, 90, 160])
        self.assertEqual(doc.value['blocks'][0]['id'], 'b1')
        self.assertFalse(any(i['requires_ocr'] for i in doc.value['quality']['issues']))

    def test_failure_retains_native_and_requires_ocr_without_retry(self):
        doc, data = document()
        native = copy.deepcopy(doc.value['blocks'])
        calls = []
        def fail(*args):
            calls.append(1)
            raise ocr.OCRFailure('timeout')
        ocr.enrich(doc, data, identity(), infer_page=fail)
        self.assertEqual(len(calls), 1)
        self.assertEqual(doc.value['blocks'], native)
        self.assertEqual(doc.value['quality']['state'], 'needs_ocr')
        self.assertEqual(doc.value['ocr']['pages'][0]['reason'], 'timeout')

    def test_table_merges_and_code_indentation_preserved(self):
        page = {'number': 1, 'width': 200, 'height': 300, 'coordinate_system': 'pdf-points-top-left'}
        raw = ('<div data-label="Table" data-bbox="0 0 1000 500"><table>'
               '<tr><th rowspan="2">A</th><td>B</td></tr><tr><td>C</td></tr></table></div>'
               '<div data-label="Code-Block" data-bbox="0 500 1000 1000"><pre>def run():\n    return 1</pre></div>')
        blocks, issues = ocr.parse_blocks(raw, page)
        self.assertEqual(blocks[0]['cells'][0]['row_span'], 2)
        self.assertEqual(blocks[0]['cells'][2]['column'], 1)
        self.assertEqual(blocks[1]['text'], 'def run():\n    return 1')
        self.assertIn('ocr_table_cell_geometry_unavailable', issues)

    def test_invalid_geometry_scripts_and_table_spans_preserve_raw_failure(self):
        page = {'number': 1, 'width': 200, 'height': 300, 'coordinate_system': 'image-pixels-top-left'}
        for raw in ('<div data-label="Text" data-bbox="0 0 1001 500">bad bbox</div>',
                    '<div data-label="Text" data-bbox="0 0 1000 500"><script>secret</script>text</div>',
                    '<div data-label="Table" data-bbox="0 0 1000 500"><table><tr><td rowspan="999999">bad span</td></tr></table></div>'):
            with self.subTest(raw=raw), self.assertRaises(ocr.OCRFailure) as caught:
                ocr.parse_blocks(raw, page)
            self.assertEqual(caught.exception.raw_html, raw)

    def test_safe_html_removes_event_handlers_and_links(self):
        raw = '<div data-label="Text" data-bbox="0 0 1000 1000"><p onclick="evil()"><a href="https://example.invalid">hello</a></p></div>'
        blocks, _ = ocr.parse_blocks(raw, {'number': 1, 'width': 10, 'height': 10, 'coordinate_system': 'image-pixels-top-left'})
        self.assertNotIn('onclick', blocks[0]['html'])
        self.assertNotIn('href', blocks[0]['html'])
        self.assertIn('hello', blocks[0]['text'])

    def test_clean_pdf_does_not_make_inference_call(self):
        doc, data = document(pdf_fixture(), 'pdf')
        native = copy.deepcopy(doc.value['blocks'])
        with patch.object(ocr, 'infer', side_effect=AssertionError('unnecessary OCR')):
            ocr.enrich(doc, data, identity())
        self.assertEqual(doc.value['blocks'], native)
        self.assertEqual(doc.value['ocr']['pages'], [])

    def test_borderless_table_is_selected_and_failure_preserves_native_text(self):
        doc, data = document(borderless_fixture(), 'pdf')
        native = copy.deepcopy(doc.value['blocks'])
        infer = unittest.mock.Mock(side_effect=ocr.OCRFailure('timeout'))
        ocr.enrich(doc, data, identity(), infer_page=infer)
        self.assertEqual(infer.call_count, 1)
        self.assertEqual(doc.value['blocks'], native)
        self.assertEqual(doc.value['ocr']['pages'][0]['reason'], 'timeout')
        self.assertEqual(doc.value['quality']['state'], 'needs_ocr')

    def test_borderless_table_success_keeps_native_evidence(self):
        doc, data = document(borderless_fixture(), 'pdf')
        native = copy.deepcopy(doc.value['blocks'])
        ocr.enrich(doc, data, identity(), infer_page=answer)
        record = doc.value['ocr']['pages'][0]
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(record['native_blocks'], native)
        self.assertEqual(doc.value['quality']['state'], 'needs_review')

    def test_pdf_raster_and_source_coordinates(self):
        doc, data = document(pdf_fixture(text=False), 'pdf')
        ocr.enrich(doc, data, identity(), infer_page=answer)
        record = doc.value['ocr']['pages'][0]
        self.assertEqual(record['status'], 'completed', record)
        self.assertGreater(record['rendered_image']['width'], 220)
        self.assertEqual(doc.value['blocks'][0]['location']['bbox'], [22, 40, 198, 160])
        self.assertEqual(doc.value['blocks'][0]['location']['coordinate_system'], 'pdf-points-top-left')

    def test_page_budget_leaves_unprocessed_pages_flagged(self):
        doc, data = document()
        doc.value['pages'].append(dict(doc.value['pages'][0], number=2))
        doc.value['blocks'].append(dict(doc.value['blocks'][0], id='b2', order=1, location={'page': 2}))
        doc.issue('image_requires_ocr', {'page': 2}, ocr=True)
        with patch.object(ocr, 'MAX_OCR_PAGES', 1):
            ocr.enrich(doc, data, identity(), infer_page=answer)
        self.assertEqual([p['status'] for p in doc.value['ocr']['pages']], ['completed', 'skipped'])
        self.assertEqual(doc.value['quality']['state'], 'needs_ocr')
        self.assertEqual(doc.value['blocks'][1]['type'], 'image')

    def test_real_http_payload_and_no_redirect_following(self):
        requests = []
        reply_code = [200]
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                raw = json.dumps(response()).encode()
                self.send_response(reply_code[0])
                self.send_header('Location', 'http://example.invalid/')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        real_connection = http.client.HTTPConnection
        def local_connection(host, port, timeout):
            self.assertEqual(host, 'model-serving-' + identity()['deployment_id'])
            self.assertEqual(port, 8000)
            return real_connection('127.0.0.1', server.server_port, timeout=timeout)
        try:
            import time
            with patch.object(ocr.http.client, 'HTTPConnection', side_effect=local_connection):
                with Image.new('RGB', (20, 20)) as image:
                    result = ocr.infer(image, identity(), time.monotonic() + 10)
                    reply_code[0] = 302
                    with self.assertRaises(ocr.OCRFailure): ocr.infer(image, identity(), time.monotonic() + 10)
            self.assertEqual(len(requests), 2)
            self.assertEqual(result['finish_reason'], 'stop')
            self.assertTrue(requests[0]['messages'][0]['content'][0]['image_url']['url'].startswith('data:image/png;base64,'))
            self.assertEqual(requests[0]['max_tokens'], 12288)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_actual_isolated_cli_loads_ocr_helper(self):
        data = pdf_fixture()
        spec = {'document_id': identity()['deployment_id'], 'sha256': hashlib.sha256(data).hexdigest(),
                'format': 'pdf', 'rules_version': parser.RULES, 'image_id': 'sha256:' + 'd' * 64, 'ocr': identity()}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'source').write_bytes(data)
            (root/'spec').write_text(json.dumps(spec))
            result = subprocess.run([sys.executable, '-I', '-B', str(Path(parser.__file__)), '--input', str(root/'source'),
                                     '--spec', str(root/'spec'), '--output', str(root/'output')], capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads((root/'output').read_bytes())
            self.assertEqual(value['processor']['version'], ocr.PROCESSOR)
            self.assertEqual(value['ocr']['identity'], identity())



class VisualOCRTests(unittest.TestCase):
    page_html = ('<div data-label="Text" data-bbox="0 0 1000 200">Main body</div>'
                 '<div data-label="Diagram" data-bbox="100 300 800 700"><img/></div>')
    crop_html = ('<div data-label="Text" data-bbox="0 0 1000 500">Input → Process</div>'
                 '<div data-label="Table" data-bbox="0 500 1000 1000"><table>'
                 '<tr><th>Input</th><th>Amount</th></tr><tr><td>Water</td><td>42</td></tr></table></div>')
    def reply(self, raw):
        return ocr.decode_response(json.dumps(response(raw)).encode(), identity()['served_model_name'])
    def test_page_and_partial_crop_duplicate_retains_one_traceable_copy(self):
        doc, data = document(); calls = []
        page_html = (self.page_html + '<div data-label="Caption" data-bbox="100 620 800 700">Figure caption</div>')
        crop_html = ('<div data-label="Diagram" data-bbox="0 0 1000 800"><img/></div>'
                     '<div data-label="Caption" data-bbox="0 800 1000 1000">Figure caption</div>')
        def infer(*args):
            calls.append(1); return self.reply(page_html if len(calls) == 1 else crop_html)
        ocr.enrich(doc, data, identity(), infer_page=infer)
        page = doc.value['ocr']['pages'][0]
        self.assertEqual(len(calls), 2)
        self.assertEqual(sum(b.get('text') == 'Figure caption' for b in doc.value['blocks']), 1)
        kept = next(b for b in doc.value['blocks'] if b.get('text') == 'Figure caption')
        self.assertEqual(kept['visual_region_id'], 'v1')
        self.assertEqual(page['visual_regions'][0]['result_blocks'], 1)
        self.assertEqual(page['visual_regions'][0]['status'], 'partial')
        self.assertEqual(page['deduplicated_page_blocks'][0]['removed']['text'], 'Figure caption')
        self.assertEqual(page['deduplicated_page_blocks'][0]['kept_model_bbox'], kept['model_bbox'])
        self.assertEqual(page['unresolved_visual_blocks'], 1)
        self.assertEqual(doc.value['quality']['state'], 'needs_ocr')

    def test_visual_dedup_preserves_changed_values_distant_text_and_ambiguous_matches(self):
        page = {'type': 'paragraph', 'label': 'Text', 'text': 'Price 1.20', 'extraction': 'surya-openai-v1',
                'location': {'page': 1, 'bbox': [10, 10, 50, 20], 'coordinate_system': 'pdf-points-top-left'}}
        crop = dict(copy.deepcopy(page), visual_region_id='v1', model_bbox=[0, 0, 1000, 1000])
        for variant in ('number', 'case', 'substring', 'far', 'page', 'coordinates', 'large', 'type', 'label', 'multiple_page', 'multiple_crop'):
            with self.subTest(variant=variant):
                a, b = copy.deepcopy(page), copy.deepcopy(crop)
                if variant == 'number': b['text'] = 'Price 1.21'
                if variant == 'case': b['text'] = 'price 1.20'
                if variant == 'substring': b['text'] += ' Kg'
                if variant == 'far': b['location']['bbox'] = [10, 40, 50, 50]
                if variant == 'page': b['location']['page'] = 2
                if variant == 'coordinates': b['location']['coordinate_system'] = 'image-pixels-top-left'
                if variant == 'large': b['location']['bbox'] = [0, 0, 100, 200]
                if variant == 'type': b['type'] = 'table'
                if variant == 'label': b['label'] = 'Caption'
                blocks = [a, b]
                if variant == 'multiple_page': blocks.append(copy.deepcopy(a))
                if variant == 'multiple_crop': blocks.append(dict(copy.deepcopy(b), visual_region_id='v2'))
                kept, evidence = ocr.deduplicate_visual_text(blocks)
                self.assertEqual(kept, blocks)
                self.assertEqual(evidence, [])
        page['text'], crop['text'] = '가 é', '가\ne\u0301'
        self.assertEqual(ocr.deduplicate_visual_text([page, crop])[0], [crop])

    def test_partial_crop_keeps_read_text_and_original_unread_region(self):
        doc, data = document(); calls = []
        raw = ('<div data-label="Diagram" data-bbox="0 0 1000 800"><img/></div>'
               '<div data-label="Caption" data-bbox="0 800 1000 1000">Figure caption</div>')
        def infer(*args):
            calls.append(1); return self.reply(self.page_html if len(calls) == 1 else raw)
        ocr.enrich(doc, data, identity(), infer_page=infer)
        page = doc.value['ocr']['pages'][0]; region = page['visual_regions'][0]
        self.assertEqual((region['status'], region['reason'], region['result_blocks']), ('partial', 'visual_text_unread', 1))
        self.assertEqual((page['status'], page['unresolved_visual_blocks']), ('partial', 1))
        self.assertEqual(doc.value['quality']['state'], 'needs_ocr')
        self.assertEqual(len(calls), 2)
        blocks = doc.value['blocks']
        self.assertEqual([b.get('text') for b in blocks], ['Main body', '', 'Figure caption'])
        self.assertEqual(blocks[1]['recognition_status'], 'unread')
        self.assertEqual(blocks[1]['location'], region['source_location'])
        self.assertEqual(blocks[2]['visual_region_id'], region['id'])
        self.assertEqual(blocks[2]['location']['bbox'], [10, 124, 80, 140])
        self.assertIn('Figure caption', region['raw_html'])

    def test_partial_crop_with_only_picture_page_preserves_table_and_unread_status(self):
        doc, data = document(); calls = []
        page = '<div data-label="Image" data-bbox="0 0 1000 1000"><img/></div>'
        def infer(*args):
            calls.append(1)
            return self.reply(page if len(calls) == 1 else self.crop_html + page)
        ocr.enrich(doc, data, identity(), infer_page=infer)
        record = doc.value['ocr']['pages'][0]
        self.assertEqual(record['visual_regions'][0]['status'], 'partial')
        self.assertEqual(record['visual_regions'][0]['result_blocks'], 2)
        self.assertEqual(record['status'], 'partial')
        self.assertEqual(sum(b.get('recognition_status') == 'unread' for b in doc.value['blocks']), 1)
        self.assertEqual(next(b for b in doc.value['blocks'] if b['type'] == 'table')['cells'][-1]['text'], '42')
        self.assertEqual(len(calls), 2)
    def test_crop_text_and_table_replace_only_visual_and_map_back_to_original(self):
        doc, data = document()
        native = copy.deepcopy(doc.value['blocks']); calls=[]
        def infer(image, pinned, deadline):
            calls.append((image.size, pinned))
            return self.reply(self.page_html if len(calls)==1 else self.crop_html)
        ocr.enrich(doc,data,identity(),infer_page=infer,measure_timings=True)
        page=doc.value['ocr']['pages'][0];region=page['visual_regions'][0]
        self.assertEqual(page['status'],'completed')
        self.assertEqual(region['status'],'completed')
        self.assertEqual(region['result_blocks'],2)
        self.assertEqual(page['native_blocks'],native)
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[0][1],calls[1][1])
        self.assertEqual(doc.value['blocks'][0]['text'],'Main body')
        self.assertEqual(doc.value['blocks'][1]['location']['bbox'],[10,60,80,100])
        self.assertEqual(doc.value['blocks'][2]['location']['bbox'],[10,100,80,140])
        self.assertEqual(doc.value['blocks'][2]['cells'][-1]['text'],'42')
        self.assertTrue(all(b['visual_region_id']=='v1' for b in doc.value['blocks'][1:]))
        self.assertEqual(doc.value['quality']['state'],'needs_review')
        self.assertFalse(any(i['requires_ocr'] for i in doc.value['quality']['issues']))
        self.assertEqual(doc.value['source']['sha256'],hashlib.sha256(data).hexdigest())
    def test_picture_only_page_can_recover_without_claiming_blank_picture_success(self):
        doc,data=document();calls=[]
        def infer(*args):
            calls.append(1)
            return self.reply('<div data-label="Image" data-bbox="0 0 1000 1000"></div>' if len(calls)==1 else self.crop_html)
        ocr.enrich(doc,data,identity(),infer_page=infer)
        self.assertEqual(doc.value['ocr']['pages'][0]['status'],'completed')
        self.assertEqual(len(calls),2)
    def test_bad_crop_preserves_body_and_unread_region_without_recursive_retry(self):
        for bad in ('timeout','low_probability','unsafe','bad_box','empty_visual'):
            with self.subTest(bad=bad):
                doc,data=document();calls=[]
                def infer(*args):
                    calls.append(1)
                    if len(calls)==1:return self.reply(self.page_html)
                    if bad=='timeout':raise ocr.OCRFailure('timeout')
                    if bad=='low_probability':return dict(self.reply(self.crop_html),page_mean_token_probability=0.1)
                    if bad=='unsafe':return self.reply('<div data-label="Text" data-bbox="0 0 1000 1000"><script>x</script>text</div>')
                    if bad=='bad_box':return self.reply('<div data-label="Text" data-bbox="0 0 1001 1000">text</div>')
                    return self.reply('<div data-label="Diagram" data-bbox="0 0 1000 1000"><img/></div>')
                ocr.enrich(doc,data,identity(),infer_page=infer)
                page=doc.value['ocr']['pages'][0]
                self.assertEqual(len(calls),2)
                self.assertEqual(page['status'],'partial')
                self.assertEqual(page['visual_regions'][0]['status'],'failed')
                self.assertEqual(doc.value['blocks'][0]['text'],'Main body')
                self.assertEqual(doc.value['blocks'][1]['recognition_status'],'unread')
                self.assertEqual(doc.value['quality']['state'],'needs_ocr')
    def test_request_limit_keeps_remaining_visual_unread(self):
        doc,data=document();calls=[]
        raw=self.page_html+'<div data-label="Image" data-bbox="100 800 900 950"></div>'
        def infer(*args):
            calls.append(1);return self.reply(raw if len(calls)==1 else self.crop_html)
        with patch.object(ocr,'MAX_VISUAL_REGIONS',1):ocr.enrich(doc,data,identity(),infer_page=infer)
        page=doc.value['ocr']['pages'][0]
        self.assertEqual(len(calls),2)
        self.assertEqual([r['status'] for r in page['visual_regions']],['completed','skipped'])
        self.assertEqual(page['unresolved_visual_blocks'],1)
    def test_pages_run_before_crops_and_crop_budget_never_retries(self):
        doc,data=document();doc.value['pages'].append(dict(doc.value['pages'][0],number=2))
        doc.issue('image_requires_ocr',{'page':2},ocr=True);sizes=[]
        def infer(image,*args):
            sizes.append(image.size);return self.reply(self.page_html if len(sizes)<=2 else self.crop_html)
        ocr.enrich(doc,data,identity(),infer_page=infer)
        self.assertEqual(sizes[:2],[(100,200),(100,200)])
        self.assertEqual(len(sizes),4)
        self.assertTrue(all(p['status']=='completed' for p in doc.value['ocr']['pages']))
        doc,data=document();calls=[]
        def no_time(*args):calls.append(1);return self.reply(self.page_html)
        with patch.object(ocr,'VISUAL_BUDGET_SECONDS',0):ocr.enrich(doc,data,identity(),infer_page=no_time)
        self.assertEqual(len(calls),1)
        self.assertEqual(doc.value['ocr']['pages'][0]['visual_regions'][0]['status'],'skipped')
    def test_real_crop_pixels_and_geometry_guards(self):
        image=Image.new('RGB',(2000,2000),'white')
        image.paste('red',(100,200,1800,1800))
        page={'number':1,'width':2000,'height':2000,'coordinate_system':'image-pixels-top-left'}
        loc={'page':1,'coordinate_system':page['coordinate_system'],'bbox':[100,200,1800,1800]}
        crop,actual=ocr.render_visual(image,page,loc)
        self.assertEqual(crop.size,(1700,1600));self.assertEqual(crop.getpixel((0,0)),(255,0,0))
        self.assertEqual(actual,loc);crop.close()
        for change in ({'page':2},{'bbox':[-1,0,10,10]},{'bbox':[0,0,float('nan'),10]}, {'coordinate_system':'wrong'}):
            with self.assertRaises(ocr.OCRFailure):ocr.render_visual(image,page,dict(loc,**change))
        image.close()

if __name__ == '__main__': unittest.main()
