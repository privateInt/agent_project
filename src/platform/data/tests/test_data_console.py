"""UI/API contract tests with controlled responses. No Jobs or GPU access."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from email import policy
from email.parser import BytesParser
from unittest.mock import patch

ROOT = Path(os.environ.get('DATA_CONSOLE_SOURCE_DIR', Path(__file__).resolve().parents[1] / 'console'))
sys.path.insert(0, str(ROOT))
import data_api
import data_state
import data_results
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_data_preview import pdf_fixture, workbook

# Missing Streamlit is a failure: execute this test in the supplied test image.
from streamlit.testing.v1 import AppTest

SOURCE = '814748f8-9b33-442f-95f6-0e584760d00a'
SOURCE2 = '602b8e20-9933-4930-abf5-865bbaaf810b'
RUN = '1b1233e4-5c6c-408a-919e-5ca7ce4a2610'
REFINE = '1dea5f2f-75fd-4926-b68e-edd0f2481ad5'
OP = '8ced838f-f59e-4054-b90f-e54c84e8853e'
DEP = '29aac028-c0ad-49f6-a3fa-c2b3b67feade'
CANON = 'd3762717-7635-451d-824b-e436691ccea4'
BASE = 'http://data-gateway:8000'
GROUP = '66160185-f97c-43c0-838c-9cdf10fc986c'
GROUP2 = '77e3d03f-6bd6-485d-9b6a-31e3bdc946d2'
GROUP_RESPONSE = data_api.API.group_response
EVIL = '<script>alert("ocr")</script><img src="https://untrusted.invalid/x">'


class IntentTests(unittest.TestCase):
    def test_ticket_has_no_inputs_and_rejects_changed_request_or_gateway(self):
        body = {'name': 'private name', 'url': 'https://example.com/private', 'options': {'max_documents': 3}}
        t = data_state.ticket('crawl', body, BASE)
        restored = data_state.unpack(data_state.pack(t))
        self.assertEqual(restored, t)
        self.assertNotIn('private', json.dumps(t))
        data_state.match(t, 'crawl', body, BASE)
        for changed in [dict(body, name='changed'), dict(body, options={'max_documents': 4})]:
            with self.assertRaises(ValueError): data_state.match(t, 'crawl', changed, BASE)
        with self.assertRaises(ValueError): data_state.match(t, 'crawl', body, 'http://another:8000')

    def test_upload_recovery_requires_original_bytes_and_filename(self):
        body = {'name': '한글 문서', 'filename': '원본.txt'}
        t = data_state.ticket('upload', body, BASE, b'original')
        data_state.match(t, 'upload', body, BASE, b'original')
        for altered, raw in [(body, b'changed'), (dict(body, filename='other.txt'), b'original')]:
            with self.assertRaises(ValueError): data_state.match(t, 'upload', altered, BASE, raw)
        for filename in ['../a.txt', 'a\nb.txt', 'a".txt', 'a\\b.txt']:
            with self.assertRaises(ValueError): data_state.ticket('upload', dict(body, filename=filename), BASE, b'a')

    def test_refinement_identity_and_corrupt_ticket_fail_closed(self):
        body = {'name': 'refine', 'document_id': SOURCE, 'ocr_deployment_id': DEP}
        t = data_state.ticket('refine', body, BASE)
        with self.assertRaises(ValueError): data_state.match(t, 'refine', dict(body, document_id=SOURCE2), BASE)
        with self.assertRaises(ValueError): data_state.match(t, 'refine', dict(body, ocr_deployment_id=SOURCE), BASE)
        for raw in ['', '{}', 'a' * 1201, data_state.pack(dict(t, key='bad')), data_state.pack(dict(t, extra='injected'))]:
            with self.assertRaises(ValueError): data_state.unpack(raw)

    def test_domain_results_do_not_invent_operation_success(self):
        self.assertEqual(data_state.row_state({'output_document_id': SOURCE}), '결과 있음')
        self.assertEqual(data_state.row_state({'status': 'running', 'output_document_id': SOURCE}), '처리 중')
        self.assertEqual(data_state.row_state({'state': 'needs_review'}), '내용 확인 필요')


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.api = data_api.API(BASE)
        self.requests = []
        self.response = b'{}'
        test = self
        class Opener:
            def open(self, request, timeout):
                test.requests.append(request)
                if isinstance(test.response, Exception): raise test.response
                return io.BytesIO(test.response)
        self.api.opener = Opener()

    def test_assign_group_sends_one_scoped_request_and_checks_receipt(self):
        group = {'group_id': GROUP, 'name': '프로젝트 A', 'version': 1}
        self.response = data_api.encode({'document_id': SOURCE, 'group': group})
        self.assertEqual(self.api.assign_group(SOURCE, '  프로젝트 A ', OP), group)
        req = self.requests[-1]
        self.assertEqual((req.method, req.full_url, req.get_header('Idempotency-key')),
                         ('PUT', BASE + '/api/source-documents/' + SOURCE + '/group', OP))
        self.assertEqual(json.loads(req.data), {'name': '프로젝트 A'})
        for receipt in ({'document_id': SOURCE2, 'group': group},
                        {'document_id': SOURCE, 'group': dict(group, name='another')},
                        {'document_id': SOURCE, 'group': None}):
            self.response = data_api.encode(receipt)
            with self.assertRaises(data_api.APIError): self.api.assign_group(SOURCE, '프로젝트 A', OP)
        before = len(self.requests)
        with self.assertRaises(ValueError): self.api.assign_group('../all', 'A', OP)
        with self.assertRaises(ValueError): self.api.assign_group(SOURCE, ' ', OP)
        self.assertEqual(before, len(self.requests))

    def test_group_api_scope_replay_identity_and_receipts(self):
        self.response = data_api.encode({'group_id': GROUP, 'name': '자료 묶음', 'version': 1})
        self.api.create_group('자료 묶음', OP)
        req = self.requests[-1]
        self.assertEqual((req.method, req.full_url, req.get_header('Idempotency-key')),
                         ('POST', BASE + '/api/source-groups', OP))
        self.api.rename_group(GROUP, '바꾼 이름', 1)
        self.assertEqual(json.loads(self.requests[-1].data), {'name': '바꾼 이름', 'expected_version': 1})
        for present in (True, False):
            self.response = data_api.encode({'group_id': GROUP, 'document_id': SOURCE, 'present': present})
            self.api.group_membership(GROUP, SOURCE, present)
            req = self.requests[-1]
            self.assertEqual(req.full_url, BASE + '/api/source-groups/' + GROUP + '/documents/' + SOURCE)
            self.assertEqual(req.method, 'PUT' if present else 'DELETE')
            self.assertIsNone(req.data)
        for receipt in ({'group_id': GROUP2, 'document_id': SOURCE, 'present': False},
                        {'group_id': GROUP, 'document_id': SOURCE2, 'present': False},
                        {'group_id': GROUP, 'document_id': SOURCE, 'present': 0}):
            self.response = data_api.encode(receipt)
            with self.assertRaises(data_api.APIError): self.api.group_membership(GROUP, SOURCE, False)
        before = len(self.requests)
        with self.assertRaises(ValueError): self.api.group_membership('../all', SOURCE, False)
        with self.assertRaises(ValueError): self.api.delete('source-groups', GROUP)
        with self.assertRaises(ValueError): self.api.purge('source-groups', GROUP)
        with self.assertRaises(ValueError): self.api.listing('refinement-runs', group_id=GROUP)
        self.assertEqual(len(self.requests), before)
        self.response = data_api.encode({'items': [], 'next_cursor': ''})
        self.api.listing('source-documents', group_id=GROUP, q='자료')
        self.assertIn('group_id=' + GROUP, self.requests[-1].full_url)

    def test_crawl_and_refinement_have_correct_body_and_one_key(self):
        for kind, body, field, collection in [
            ('crawl', {'name': 'crawl', 'url': 'https://example.com', 'options': {'render_javascript': True}}, 'crawl_run_id', 'crawl-runs'),
            ('refine', {'name': 'refine', 'document_id': SOURCE, 'ocr_deployment_id': DEP}, 'refinement_run_id', 'refinement-runs')]:
            self.response = data_api.encode({field: RUN, 'operation_id': OP})
            self.api.create(kind, body, OP)
            req = self.requests[-1]
            self.assertEqual(req.full_url, BASE + '/api/' + collection)
            self.assertEqual(req.get_header('Idempotency-key'), OP)
            self.assertEqual(json.loads(req.data), body)

    def test_multipart_preserves_korean_filename_and_exact_bytes(self):
        body = {'name': '한글\r\n문서', 'filename': '원본.txt'}
        raw = b'\x00original\r\n\xff'
        self.response = data_api.encode({'document_id': SOURCE})
        self.api.create('upload', body, OP, raw)
        req = self.requests[-1]
        message = BytesParser(policy=policy.default).parsebytes(('Content-Type: ' + req.get_header('Content-type') + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + req.data)
        parts = list(message.iter_parts())
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0].get_payload(decode=True).decode(), body['name'])
        self.assertEqual(parts[1].get_filename(), body['filename'])
        self.assertEqual(parts[1].get_payload(decode=True), raw)

    def test_download_checks_both_hash_and_size_and_limits_read(self):
        self.response = b'original'
        meta = {'size_bytes': 8, 'sha256': hashlib.sha256(self.response).hexdigest()}
        self.assertEqual(self.api.download('source-documents', SOURCE, meta), b'original')
        for bad in [dict(meta, sha256='0' * 64), dict(meta, size_bytes=7), dict(meta, size_bytes=9), dict(meta, size_bytes=0), dict(meta, size_bytes=(32 << 20) + 1)]:
            with self.assertRaises(data_api.APIError): self.api.download('source-documents', SOURCE, bad)
        self.api.download('source-documents', SOURCE, meta, 'http-response')
        self.assertTrue(self.requests[-1].full_url.endswith('/http-response'))

    def test_delete_is_scoped_and_requires_exact_identity_and_deletion_time(self):
        for collection, field, id in [('source-documents', 'document_id', SOURCE), ('refinement-runs', 'refinement_run_id', REFINE)]:
            self.response = data_api.encode({field: id, 'deleted_at': '2026-09-16T10:00:00Z'})
            self.assertEqual(self.api.delete(collection, id)[field], id)
            self.assertEqual(self.requests[-1].method, 'DELETE')
            self.assertEqual(self.requests[-1].full_url, BASE + '/api/' + collection + '/' + id)
            self.assertIsNone(self.requests[-1].data)
            for value in ({field: SOURCE2, 'deleted_at': '2026-09-16T10:00:00Z'}, {field: id},
                          {field: id, 'deleted_at': None}, {field: id, 'deleted_at': '2026-09-16T10:00:00'}):
                self.response = data_api.encode(value)
                with self.assertRaises(data_api.APIError): self.api.delete(collection, id)
        before = len(self.requests)
        for collection in ('canonical-documents', 'operations', 'crawl-runs'):
            with self.assertRaises(ValueError): self.api.delete(collection, SOURCE)
        with self.assertRaises(ValueError): self.api.delete('source-documents', '../all')
        self.assertEqual(len(self.requests), before)

    def test_permanent_delete_uses_explicit_endpoint_and_verified_receipt(self):
        self.response = data_api.encode({'document_id': SOURCE, 'purge_state': 'purged', 'purged_at': '2026-09-16T10:00:00Z'})
        self.assertEqual(self.api.purge('source-documents', SOURCE)['purge_state'], 'purged')
        self.assertEqual(self.requests[-1].method, 'POST')
        self.assertEqual(self.requests[-1].full_url, BASE + '/api/source-documents/' + SOURCE + '/purge')
        self.assertIsNone(self.requests[-1].data)
        self.response = data_api.encode({'document_id': SOURCE, 'deleted_at': '2026-09-16T10:00:00Z'})
        with self.assertRaises(data_api.APIError): self.api.purge('source-documents', SOURCE)
        with self.assertRaises(ValueError): self.api.purge('canonical-documents', SOURCE)

    def test_deployment_lookup_is_bounded_get_and_checks_identity(self):
        with patch.object(self.api, '_request', return_value=data_api.encode({'deployment_id': DEP})) as request:
            self.assertEqual(self.api.deployment(DEP)['deployment_id'], DEP)
            self.assertEqual(request.call_args.args, ('GET', '/api/deployments/' + DEP))
            self.assertEqual(request.call_args.kwargs['timeout'], 5)
        for value in ({}, {'deployment_id': SOURCE}):
            self.response = data_api.encode(value)
            with self.assertRaises(data_api.APIError): self.api.deployment(DEP)

    def test_deployment_listing_pins_ready_filters_and_validates_shape(self):
        self.response = data_api.encode({'items': [{'deployment_id': DEP}], 'next_cursor': 'next'})
        self.assertEqual(self.api.deployment_listing('a+b/=')['next_cursor'], 'next')
        from urllib.parse import urlsplit, parse_qs
        query = parse_qs(urlsplit(self.requests[-1].full_url).query)
        self.assertEqual(query, {'status': ['ready'], 'runtime_state': ['ready'], 'limit': ['100'], 'cursor': ['a+b/=']})
        self.assertEqual(self.requests[-1].method, 'GET')
        for value in ({'items': [{}]}, {'items': [{'deployment_id': DEP}] * 2},
                      {'items': None}, {'items': [], 'next_cursor': 2}):
            self.response = data_api.encode(value)
            with self.assertRaises(data_api.APIError): self.api.deployment_listing()

    def test_untrusted_paths_origins_and_redirects_rejected(self):
        for base in ['file:///etc/passwd', 'http://user:pass@host', 'https://host/path', 'https://host?token=secret']:
            with self.assertRaises(ValueError): data_api.API(base)
        with self.assertRaises(ValueError): self.api.get('source-documents', '../operations')
        with self.assertRaises(ValueError): self.api.listing('source-documents', unsupported='a')
        self.assertIsNone(data_api.NoRedirect().redirect_request(None, None, None, None, None, None))
        self.assertFalse(self.requests)

    def test_errors_and_malformed_acceptance_cannot_look_successful(self):
        self.response = urllib.error.HTTPError(BASE, 429, 'rate limit', {}, io.BytesIO(b'{"error_code":"ResourceExhausted","error_message":"busy"}'))
        with self.assertRaises(data_api.APIError) as cm: self.api.get('source-documents', SOURCE)
        self.assertEqual((cm.exception.status, cm.exception.code), (429, 'ResourceExhausted'))
        for raw in [b'not json', b'[]', b'{"crawl_run_id":"bad"}', data_api.encode({'crawl_run_id': RUN})]:
            self.response = raw
            with self.assertRaises(data_api.APIError): self.api.create('crawl', {'name': 'x'}, OP)

    def test_gateway_upload_rejection_keeps_its_original_reason(self):
        message = 'file structure does not match its extension'
        self.response = urllib.error.HTTPError(BASE, 400, 'bad request', {}, io.BytesIO(data_api.encode(
            {'error_code': 'InvalidArgument', 'error_message': message})))
        with self.assertRaises(data_api.APIError) as cm:
            self.api.create('upload', {'name': 'sample', 'filename': 'sample.pdf'}, OP, b'original')
        self.assertEqual((cm.exception.status, cm.exception.code, str(cm.exception)), (400, 'InvalidArgument', message))
        self.assertIn('파일 형식 검사', data_state.friendly_error(cm.exception))

    def test_cursor_and_search_are_query_encoded(self):
        self.response = b'{"items":[],"next_cursor":""}'
        self.api.listing('source-documents', q='a&status=failed', cursor='next+page/=', limit=10)
        from urllib.parse import parse_qs, urlsplit
        q = parse_qs(urlsplit(self.requests[-1].full_url).query)
        self.assertEqual(q, {'q': ['a&status=failed'], 'cursor': ['next+page/='], 'limit': ['10']})


class PresentationTests(unittest.TestCase):
    def test_partial_page_is_distinct_and_visual_warning_survives_markdown(self):
        value = {'ocr': {'pages': [{'status': 'completed'}, {'status': 'partial'}]},
                 'quality': {'issues': [{'code': 'ocr_visual_content_unread', 'requires_ocr': True,
                                        'location': {'page': 3}}]},
                 'blocks': [{'type': 'text', 'text': '읽힌 본문', 'location': {'page': 3}},
                            {'type': 'image', 'recognition_status': 'unread', 'location': {'page': 3}}]}
        self.assertEqual(data_results.recognition_summary(value), '글자 인식 결과: 완료 1개 페이지 · 부분 인식 1개 페이지')
        result = data_results.markdown_document(value)
        self.assertIn('부분 인식', result)
        self.assertIn('그림·도표 내부의 글자를 읽지 못했습니다', result)
        self.assertIn('읽힌 본문', result)
        self.assertIn('그림 영역 1개', result)

    def test_timing_display_uses_measured_fields_and_rejects_invalid_or_missing_times(self):
        self.assertEqual(data_results.timing_rows({}), [])
        op = {'created_at': '2026-09-16T00:00:00.123456789Z',
              'execution_started_at': '2026-09-16T00:00:03.123456789Z',
              'completed_at': '2026-09-16T00:02:00.123456789Z'}
        value = {'timings': {'version': 'refinement-timing-v1', 'native_parse_ms': 1250,
                             'ocr_ms': 95000, 'parser_total_ms': 96250},
                 'ocr': {'pages': [{'page': 1, 'status': 'partial',
                        'timings': {'render_ms': 30, 'request_ms': 45000, 'total_ms': 45040}}]}}
        self.assertEqual([r['시간(초)'] for r in data_results.timing_rows(value, op)], [120, 3, 117, 1.25, 95, 96.25])
        row = data_results.page_timing_rows(value)[0]
        self.assertEqual(row['결과'], '부분 인식')
        self.assertIsNone(row['응답 정리(초)'])
        value['timings']['native_parse_ms'] = float('nan')
        value['timings']['ocr_ms'] = -1
        value['timings']['parser_total_ms'] = True
        self.assertEqual(data_results.timing_rows(value, {'created_at': 'bad'}), [])

    def test_fragmented_images_have_one_count_without_mutating_json(self):
        blocks = [{'type': 'image', 'location': {'page': 3}} for _ in range(882)]
        blocks.insert(400, {'type': 'text', 'text': '중간 내용', 'location': {'page': 3}})
        blocks.extend([{'type': 'image', 'text': '그림 설명', 'location': {'page': 3}},
                       {'type': 'image', 'location': {'page': 4}}])
        value = {'blocks': blocks}
        before = json.dumps(value)
        markdown = data_results.markdown_document(value)
        view = data_results.reading_html(blocks)
        self.assertEqual(markdown.count('그림 영역'), 1)
        self.assertIn('그림 영역 883개', markdown)
        self.assertIn('3페이지: 882개', markdown)
        self.assertIn('4페이지: 1개', markdown)
        self.assertNotIn('원본 자료에서 확인할 수 있습니다.', markdown)
        self.assertNotIn('[그림 영역', view)
        for result in (markdown, view):
            self.assertIn('중간 내용', result)
            self.assertIn('그림 설명', result)
        self.assertEqual(len(data_results.visible_blocks(blocks)), 2)
        self.assertEqual(json.dumps(value), before)

    def test_markdown_quotes_preserve_text_without_broken_numeric_entities(self):
        value = {'blocks': [{'type': 'text', 'text': '''author's "quote" <script> & value'''}]}
        result = data_results.markdown_document(value)
        self.assertIn('''author's "quote"''', result)
        self.assertNotIn('&#x27;', result)
        self.assertNotIn('<script>', result)
        self.assertIn('&lt;script&gt; &amp; value', result)

    def test_empty_image_count_keeps_descriptions_tables_and_locationless_regions(self):
        value = {'blocks': [{'type': 'image', 'location': {'page': 1}} for _ in range(3)] +
                 [{'type': 'image', 'location': {'page': 3}}, {'type': 'image'},
                  {'type': 'image', 'text': '설명 보존'},
                  {'type': 'image', 'cells': [{'row': 0, 'column': 0, 'text': '표 보존'}]}]}
        summary = data_results.image_summary(value['blocks'])
        self.assertEqual(summary, '그림 영역 5개 (1페이지: 3개, 3페이지: 1개, 위치 미지정: 1개)')
        result = data_results.markdown_document(value)
        self.assertEqual(result.count('그림 영역'), 1)
        for text in ('설명 보존', '표 보존'): self.assertIn(text, result)
        self.assertEqual(data_results.image_summary([{'type': 'text', 'text': 'hello'}]), '')

    def test_markdown_keeps_full_tables_pages_and_escaped_model_text(self):
        cells = [{'row': r, 'column': 0, 'text': '값' + str(r)} for r in range(250)]
        cells[0].update(text=EVIL + '\n' + '긴값' * 2500, column_span=2)
        value = {'blocks': [
            {'type': 'heading', 'level': 2, 'text': '분기 실적', 'location': {'page': 2}},
            {'type': 'table', 'cells': cells, 'location': {'page': 2}},
            {'type': 'code', 'text': '```\n원본 코드', 'location': {'page': 3}}]}
        result = data_results.markdown_document(value)
        self.assertIn('## 2페이지', result)
        self.assertIn('## 분기 실적', result)
        self.assertIn('## 3페이지', result)
        self.assertIn('병합 셀의 값', result)
        self.assertNotIn('<table>', result)
        self.assertIn('값249', result)
        self.assertIn('긴값' * 2500, result)
        self.assertIn('&lt;script&gt;', result)
        self.assertNotIn('<script>', result)
        self.assertIn('````\n```\n원본 코드\n````', result)
        self.assertNotIn('일부만 표시', result)

    def test_markdown_sparse_ambiguous_tables_keep_every_cell_without_expanding_grid(self):
        table = {'type': 'table', 'merged_ranges': ['A1:C1'], 'cells': [
            {'row': 0, 'column': 0, 'value': '첫 값'},
            {'row': 1000000, 'column': 10000, 'value': '마지막 값'}]}
        result = data_results.markdown_document({'blocks': [table]})
        self.assertLess(len(result), 2000)
        for term in ('첫 값', '마지막 값', '1000000', '10000', 'A1:C1'):
            self.assertIn(term, result)
        table['cells'].append({'row': 0, 'column': 0, 'value': '중복 위치 값'})
        self.assertIn('중복 위치 값', data_results.markdown_document({'blocks': [table]}))

    def test_markdown_keeps_xlsx_merges_and_quality_caveats(self):
        value = {'quality': {'issues': [{'code': 'ocr_timeout', 'requires_ocr': True, 'location': {'page': 2}}]},
                 'blocks': [{'type': 'table', 'location': {'sheet': '결산'}, 'merged_ranges': ['B2:C3'],
                             'cells': [{'row': 1, 'column': 1, 'value': '병합된 값'}]}]}
        result = data_results.markdown_document(value)
        self.assertIn('확인이 필요한 부분', result)
        self.assertIn('페이지 2', result)
        self.assertIn('## 결산', result)
        self.assertIn('| 병합된 값 | 병합된 값 |', result)
        self.assertEqual(result.count('병합된 값'), 4)
    def test_recognition_summary_distinguishes_completed_failed_and_skipped(self):
        self.assertEqual(data_results.recognition_summary({}), '')
        self.assertEqual(data_results.recognition_summary({'ocr': {'pages': []}}), '')
        value = {'ocr': {'pages': [{'status': 'completed'}, {'status': 'failed'}, {'status': 'skipped'}]}}
        self.assertEqual(data_results.recognition_summary(value), '글자 인식 결과: 완료 1개 페이지 · 읽기 실패 1개 페이지 · 미처리 1개 페이지')

    def test_upload_rejections_explain_format_size_encoding_and_filename(self):
        messages = {
            'file structure does not match its extension': '파일 형식 검사',
            'file exceeds the 32 MiB limit or is truncated': '32 MiB',
            'text uploads must use UTF-8': 'UTF-8',
            'filename must be a basename of 1..255 UTF-8 bytes': '파일 이름',
            'invalid DOCX/XLSX package: ZIP directory is invalid': '내부 구조',
            'unsupported file extension; use PDF': '지원하지 않는 파일 형식',
            'invalid or oversized multipart upload': '파일 전송 형식',
        }
        for raw, expected in messages.items():
            with self.subTest(raw=raw):
                self.assertIn(expected, data_state.friendly_error('InvalidArgument: ' + raw))

    def test_unknown_error_does_not_invent_a_damaged_file_diagnosis(self):
        text = data_state.friendly_error('InvalidArgument: unknown reason')
        self.assertNotIn('손상', text)
        self.assertNotIn('파일 형식 검사', text)

    def test_automatic_recognition_follows_backend_supported_formats(self):
        for kind in ['text', 'markdown', 'html', 'docx', 'xlsx', 'pdf', 'png', 'jpeg']:
            with self.subTest(format=kind):
                body = data_state.refinement_body({'document_id': SOURCE, 'format': kind}, DEP)
                self.assertEqual('ocr_deployment_id' in body, kind in ('pdf', 'png', 'jpeg'))
        self.assertEqual(data_state.refinement_body({'document_id': SOURCE, 'content_type': 'application/pdf; qs=0.001'}, DEP)['ocr_deployment_id'], DEP)
        self.assertNotIn('ocr_deployment_id', data_state.refinement_body({'document_id': SOURCE, 'format': 'png'}, ''))

    def test_canonical_html_escapes_text_and_ignores_generated_html(self):
        markup = data_results.reading_html([{'type': 'heading', 'text': '보고서'},
            {'type': 'paragraph', 'text': EVIL, 'html': '<iframe src="https://remote">bad</iframe>'}])
        self.assertIn('<h3>보고서</h3>', markup)
        self.assertIn('&lt;script&gt;', markup)
        self.assertNotIn('<iframe', markup)
        self.assertNotIn('<script', markup)
        self.assertNotIn('<img', markup)

    def test_table_positions_and_spans_become_readable_cells(self):
        table = {'cells': [
            {'row': 0, 'column': 0, 'text': '구분', 'row_span': 2},
            {'row': 0, 'column': 1, 'text': '실적', 'column_span': 2},
            {'row': 1, 'column': 1, 'text': '<img onerror="bad">'},
            {'row': 1, 'column': 2, 'value': '10'}]}
        markup = data_results.table_html(table)
        self.assertIn('rowspan="2" colspan="1">구분', markup)
        self.assertIn('rowspan="1" colspan="2">실적', markup)
        self.assertIn('&lt;img', markup)
        self.assertNotIn('<img', markup)
        self.assertEqual(markup.count('<tr>'), 2)

    def test_spreadsheet_merged_ranges_and_large_ranges_are_bounded(self):
        block = {'cells': [{'row': 0, 'column': 0, 'value': '매출'}], 'merged_ranges': ['A1:B2']}
        self.assertIn('rowspan="2" colspan="2">매출', data_results.table_html(block))
        block['cells'][0].update(row_span=1000000, column_span=1000000)
        block.pop('merged_ranges')
        markup = data_results.table_html(block)
        self.assertLess(len(markup), 5000)
        self.assertIn('일부만 표시', markup)

    def test_quality_guidance_has_meaning_and_page_locations_without_codes(self):
        result = {'quality': {'issues': [{'code': 'pdf_reading_order_is_geometric_heuristic', 'location': {'page': 3}},
            {'code': 'ocr_timeout', 'requires_ocr': True, 'location': {'page': 2}},
            {'code': 'ocr_output_requires_review', 'location': {'page': 1}}]}}
        text = '\n'.join(data_results.quality_messages(result))
        self.assertIn('문단을 읽는 순서', text)
        self.assertIn('일부 글자', text)
        self.assertIn('페이지 2', text)
        self.assertNotIn('ocr_', text)
        self.assertNotIn('pdf_', text)

    def test_text_download_contains_full_text_and_values_without_running_formulas(self):
        result = {'blocks': [{'type': 'paragraph', 'text': '가' * 22000},
                            {'type': 'table', 'cells': [{'row': 0, 'column': 0, 'value': '3', 'formula': '1+2'}, {'row': 0, 'column': 1, 'text': '결과'}]}]}
        text = data_results.plain_document(result)
        self.assertIn('가' * 22000, text)
        self.assertTrue(text.endswith('3\t결과'))
        self.assertNotIn('1+2', text)


class ConsoleUITests(unittest.TestCase):
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        self.calls = []
        self.lost = False
        self.corrupt = False
        self.paginate = False
        self.running = False
        self.reject = False
        self.rejection_message = 'invalid options'
        self.result_state = 'needs_review'
        self.missing_source = ''
        self.deleted = set()
        self.delete_lost = False
        self.delete_error = None
        self.purged = set()
        self.source_payloads = {}
        self.source_ids = None
        self.source_metadata = {}
        self.groups = {}
        self.memberships = set()
        self.group_keys = {}
        self.group_lost = False
        self.group_error = None
        self.deployment_pages = None
        self.deployment_state = {'deployment_id': DEP, 'status': 'ready', 'runtime_state': 'ready',
            'desired_state': 'running', 'artifact_kind': 'snapshot',
            'serving': {'engine': 'vllm', 'profile': 'surya-ocr-v2'}}
        self.raw = data_api.encode({'schema_version': 'canonical-document-v1', 'source': {'document_id': SOURCE},
            'processor': {}, 'cleaning': {}, 'quality': {'state': 'needs_review', 'issues': [{'code': 'review_ocr'}]},
            'pages': [{'number': 1}], 'blocks': [{'id': 'b1', 'type': 'paragraph', 'text': EVIL},
                {'id': 'b2', 'type': 'table', 'cells': [{'row': 0, 'column': 0, 'row_span': 2, 'column_span': 1, 'value': '한글'}]}]})
        test = self
        class FakeAPI:
            base = BASE
            def __init__(self, base=None):
                self.base = base or BASE
            def listing(self, collection, **filters):
                test.calls.append(('LIST', collection, filters))
                if collection == 'source-groups':
                    if test.group_error: raise test.group_error
                    return {'items': list(test.groups.values()), 'next_cursor': ''}
                id = SOURCE2 if filters.get('cursor') else SOURCE
                if collection == 'source-documents': items = [self.get(collection, source) for source in (test.source_ids if test.source_ids is not None else [id])]
                elif collection == 'crawl-runs': items = [{'crawl_run_id': RUN, 'name': EVIL, 'url': 'https://example.com/reports', 'output_document_ids': [SOURCE], 'submission_status': 'delivered'}]
                elif collection == 'refinement-runs': items = [{'refinement_run_id': REFINE, 'name': '정제', 'output_document_id': CANON}]
                else: items = [self.get(collection, OP)]
                field = {'source-documents': 'document_id', 'refinement-runs': 'refinement_run_id', 'crawl-runs': 'crawl_run_id', 'operations': 'operation_id'}[collection]
                items = [row for row in items if (collection, row[field]) not in test.purged and (filters.get('include_deleted') == 'true' or (collection, row[field]) not in test.deleted)]
                if collection == 'source-documents' and filters.get('group_id'):
                    items = [row for row in items if (filters['group_id'], row['document_id']) in test.memberships]
                return {'items': items, 'next_cursor': 'next+page/=' if test.paginate and not filters.get('cursor') else ''}
            group_response = staticmethod(GROUP_RESPONSE)
            def assign_group(self, document, name, key):
                test.calls.append(('GROUP_ASSIGN', document, name, key))
                if key not in test.group_keys:
                    group = next((g for g in test.groups.values() if g['name'] == name), None)
                    if group is None:
                        id = GROUP if GROUP not in test.groups else GROUP2
                        group = {'group_id': id, 'name': name, 'version': 1}
                        test.groups[id] = group
                    test.memberships.add((group['group_id'], document))
                    test.group_keys[key] = dict(group)
                if test.group_lost: raise data_api.APIError('lost reply', code='CONNECTION_ERROR')
                return test.group_keys[key]
            def create_group(self, name, key):
                test.calls.append(('GROUP_CREATE', name, key))
                if key not in test.group_keys:
                    id = GROUP if GROUP not in test.groups else GROUP2
                    test.groups[id] = {'group_id': id, 'name': name, 'version': 1}
                    test.group_keys[key] = dict(test.groups[id])
                if test.group_lost: raise data_api.APIError('lost reply', code='CONNECTION_ERROR')
                return test.group_keys[key]
            def rename_group(self, id, name, version):
                test.calls.append(('GROUP_RENAME', id, name, version))
                row = test.groups[id]
                if row['version'] != version and not (row['version'] == version + 1 and row['name'] == name):
                    raise data_api.APIError('stale version', 409)
                if row['name'] != name: row.update(name=name, version=row['version'] + 1)
                return dict(row)
            def group_membership(self, group, document, present):
                test.calls.append(('GROUP_MEMBER', group, document, present))
                if present: test.memberships.add((group, document))
                else: test.memberships.discard((group, document))
                if test.group_lost: raise data_api.APIError('lost reply', code='CONNECTION_ERROR')
                return {'group_id': group, 'document_id': document, 'present': present}
            def get(self, collection, id):
                test.calls.append(('GET', collection, id))
                deleted_at = '2026-09-16T10:00:00Z' if (collection, id) in test.deleted else None
                if collection == 'source-documents':
                    groups = [dict(test.groups[g]) for g, d in sorted(test.memberships) if d == id]
                    if id == test.missing_source: raise data_api.APIError('일시적인 파일 조회 오류', 503)
                    if id in test.source_payloads:
                        raw, kind = test.source_payloads[id]
                        return dict({'document_id': id, 'groups': groups, 'deleted_at': deleted_at, 'source_url': 'https://example.com/report.' + kind,
                                'format': kind, 'state': 'ready', 'size_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}, **test.source_metadata.get(id, {}))
                    return {'document_id': id, 'groups': groups, 'deleted_at': deleted_at, 'name': '원본', 'state': 'ready', 'size_bytes': 3, 'sha256': hashlib.sha256(b'dom').hexdigest(), 'original_filename': '', 'source_url': 'https://example.com/reports' if id == SOURCE else 'https://example.com/attachment.html', 'content_type': 'text/html', 'crawl_role': 'seed' if id == SOURCE else 'attachment',
                        'browser_capture': {'representation': 'rendered_dom', 'response_content_type': 'text/html', 'response_size_bytes': 3, 'response_sha256': hashlib.sha256(b'raw').hexdigest(), 'network_requests': 1, 'network_bytes': 3, 'blocked_requests': 0}}
                if collection == 'canonical-documents':
                    return {'document_id': CANON, 'state': test.result_state, 'size_bytes': len(test.raw), 'sha256': hashlib.sha256(test.raw).hexdigest(), 'page_count': 1, 'block_count': 2}
                status = 'running' if test.running else 'succeeded'
                common = {'operation_id': OP, 'status': status, 'phase': 'parsing_and_ocr' if test.running else 'completed', 'submission_status': 'delivered'}
                if collection == 'crawl-runs': return dict(common, crawl_run_id=RUN, url='https://example.com/reports', output_document_ids=[] if test.running else [SOURCE, SOURCE2])
                if collection == 'refinement-runs': return dict(common, refinement_run_id=REFINE, deleted_at=deleted_at, source_document_id=SOURCE, output_document_id='' if test.running else CANON)
                if collection == 'operations': return dict(common, resource_type='refinement_run', resource_id=REFINE, kind='refinement.parse')
                raise AssertionError(collection)
            def delete(self, collection, id):
                test.calls.append(('DELETE', collection, id))
                if test.delete_error: raise test.delete_error
                if collection == 'refinement-runs' and test.running:
                    raise data_api.APIError('still running', 409, 'FailedPrecondition')
                test.deleted.add((collection, id))
                if test.delete_lost: raise data_api.APIError('lost deletion reply', code='CONNECTION_ERROR')
                return self.get(collection, id)
            def purge(self, collection, id):
                test.calls.append(('PURGE', collection, id))
                if test.delete_error: raise test.delete_error
                test.purged.add((collection, id)); test.deleted.add((collection, id))
                if test.delete_lost: raise data_api.APIError('lost purge reply', code='CONNECTION_ERROR')
                return dict(self.get(collection, id), purge_state='purged', purged_at='2026-09-16T10:00:00Z')
            def create(self, kind, body, key, file=None):
                test.calls.append(('POST', kind, body.copy(), key, file))
                if kind == 'refine' and body.get('ocr_deployment_id'):
                    # Enforce the backend contract independently of the UI helper.
                    source_format = test.source_payloads.get(body['document_id'], (None, 'html'))[1]
                    if source_format not in ('pdf', 'png', 'jpeg'):
                        raise data_api.APIError('OCR currently accepts PNG, JPEG and PDF sources', 400, 'InvalidArgument')
                if test.reject: raise data_api.APIError(test.rejection_message, 400, 'InvalidArgument')
                if test.lost: raise data_api.APIError('lost reply', code='CONNECTION_ERROR')
                return {'crawl_run_id': RUN, 'document_id': SOURCE, 'refinement_run_id': REFINE, 'operation_id': OP}
            def download(self, collection, id, metadata, representation='content'):
                test.calls.append(('DOWNLOAD', collection, id, representation))
                raw = b'corrupt' if test.corrupt else (test.raw if collection == 'canonical-documents' else test.source_payloads[id][0] if id in test.source_payloads else b'raw' if representation == 'http-response' else b'dom')
                return data_api.verified(raw, metadata)
            def deployment_listing(self, cursor=''):
                test.calls.append(('DEPLOYMENT_LIST', cursor))
                if test.deployment_pages is not None:
                    response = test.deployment_pages[cursor]
                    if isinstance(response, Exception): raise response
                    return response
                if isinstance(test.deployment_state, Exception): raise test.deployment_state
                return {'items': [test.deployment_state], 'next_cursor': ''}
            def deployment(self, id):
                test.calls.append(('DEPLOYMENT_GET', id))
                if isinstance(test.deployment_state, Exception): raise test.deployment_state
                return test.deployment_state
        self.patches = [patch.object(data_api, 'API', FakeAPI), patch.dict(os.environ, {'DATA_UI_OCR_DEPLOYMENT_ID': DEP, 'DATA_UI_REQUEST_DIR': self.storage.name})]
        for p in self.patches: p.start()
        # Views may already be imported by a preceding AppTest.
        import data_ui
        import data_ui_actions
        import data_ui_documents
        import data_ui_deletion
        import data_ui_groups
        for module in (data_ui, data_ui_actions, data_ui_documents, data_ui_deletion, data_ui_groups):
            p = patch.object(module, 'API', FakeAPI)
            p.start()
            self.patches.append(p)
        self.app = self.new_app()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()

    def new_app(self, query=None, *, query_ocr=True):
        app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=10)
        if query:
            for k, v in query.items(): app.query_params[k] = v
        app.run()
        self.assertFalse(app.exception, [e.message for e in app.exception])
        if query_ocr:
            next(x for x in app.button if x.key == 'query_ocr_models').click().run()
            self.assertFalse(app.exception, [e.message for e in app.exception])
        return app

    def widget(self, kind, label=None, key=None):
        values = [x for x in getattr(self.app, kind) if (label is None or x.label == label) and (key is None or x.key == key)]
        self.assertEqual(len(values), 1, (kind, label, key, [x.label for x in getattr(self.app, kind)]))
        return values[0]

    def clean(self):
        self.assertFalse(self.app.exception, [e.message for e in self.app.exception])

    def click(self, label=None, key=None):
        w = self.widget('button', label, key)
        self.assertFalse(w.disabled)
        w.click().run(); self.clean()

    def page(self, page):
        self.widget('radio', key='data_nav').set_value(page).run(); self.clean()

    def choose_file(self, file_id):
        self.widget('radio', key='collection_file_' + RUN).set_value(file_id).run(); self.clean()

    def upload_form(self):
        self.click('자료 가져오기')
        self.widget('radio', key='request_method').set_value('upload').run(); self.clean()

    def posts(self):
        return [x for x in self.calls if x[0] == 'POST']

    def crawl_form(self):
        self.click('자료 가져오기')
        self.widget('text_input', key='crawl_url').set_value('https://example.com')

    def test_row_group_assign_creates_reuses_and_retries_without_jobs(self):
        self.source_ids = [SOURCE, SOURCE2]
        self.app = self.new_app(query_ocr=False)
        self.assertTrue(any(x.value == '그룹' for x in self.app.markdown))
        self.assertTrue(self.widget('button', key='row_group_remove_' + SOURCE).disabled)
        self.click(key='row_group_assign_' + SOURCE)
        self.assertFalse([c for c in self.calls if c[0] == 'GROUP_ASSIGN'])
        self.widget('text_input', key='assign_group_name').set_value(' 프로젝트 A ')
        self.group_lost = True
        self.click('지정')
        self.app.run(); self.clean()
        self.assertEqual(len([c for c in self.calls if c[0] == 'GROUP_ASSIGN']), 1)
        self.group_lost = False
        self.click('지정')
        calls = [c for c in self.calls if c[0] == 'GROUP_ASSIGN']
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(self.memberships, {(GROUP, SOURCE)})
        self.assertTrue(any(x.value == '프로젝트 A' for x in self.app.text))
        self.click(key='row_group_assign_' + SOURCE2)
        self.widget('text_input', key='assign_group_name').set_value('프로젝트 A')
        self.click('지정')
        self.assertEqual(len(self.groups), 1)
        self.assertEqual(self.memberships, {(GROUP, SOURCE), (GROUP, SOURCE2)})
        self.assertFalse(self.posts() or self.deleted or self.purged)
        self.assertFalse([c for c in self.calls if c[0] == 'DOWNLOAD'])

    def test_row_group_remove_only_selected_link_and_lost_reply_preserves_files(self):
        self.groups = {GROUP: {'group_id': GROUP, 'name': 'A', 'version': 1},
                       GROUP2: {'group_id': GROUP2, 'name': 'B', 'version': 1}}
        self.memberships = {(GROUP, SOURCE), (GROUP2, SOURCE), (GROUP2, SOURCE2)}
        self.source_ids = [SOURCE, SOURCE2]
        before_raw = self.raw
        self.app = self.new_app(query_ocr=False)
        self.widget('selectbox', key='source_group_filter').set_value(GROUP).run(); self.clean()
        self.click(key='row_group_remove_' + SOURCE)
        self.widget('selectbox', key='remove_group_target').set_value(GROUP).run(); self.clean()
        self.group_lost = True
        self.click(key='confirm_group_remove')
        self.app.run(); self.clean()
        self.assertEqual(len([c for c in self.calls if c[0] == 'GROUP_MEMBER']), 1)
        self.group_lost = False
        self.click(key='confirm_group_remove')
        self.assertEqual(self.memberships, {(GROUP2, SOURCE), (GROUP2, SOURCE2)})
        self.assertFalse(any(x.key == 'detail_' + SOURCE for x in self.app.button))
        writes = [c for c in self.calls if c[0].startswith('GROUP_')]
        self.app.run(); self.clean()
        self.assertEqual(writes, [c for c in self.calls if c[0].startswith('GROUP_')])
        self.widget('selectbox', key='source_group_filter').set_value('').run(); self.clean()
        self.assertTrue(all(any(x.key == 'detail_' + id for x in self.app.button) for id in self.source_ids))
        self.assertEqual(self.raw, before_raw)
        self.assertFalse(self.deleted or self.purged or self.posts())
        self.assertFalse([c for c in self.calls if c[0] == 'DOWNLOAD'])

    def test_row_group_empty_input_close_and_rename_column_refresh(self):
        self.click(key='row_group_assign_' + SOURCE)
        self.click('지정')
        self.assertTrue(self.app.error)
        self.assertFalse(self.groups)
        self.click(key='close_group_dialog')
        self.groups = {GROUP: {'group_id': GROUP, 'name': 'A', 'version': 1}}
        self.memberships = {(GROUP, SOURCE)}
        self.click(key='reload_groups')
        self.click(key='query_source')
        self.assertFalse(next(x for x in self.app.expander if x.label == '그룹 이름 관리').proto.expanded)
        self.widget('text_input', key='rename_group_name_' + GROUP + '_1').set_value('B')
        self.click('이름 변경')
        self.assertTrue(any(x.value == 'B' for x in self.app.text))
        self.assertFalse([x for x in self.app.button if x.label == '그룹 만들기'])

    def test_row_group_assignment_uses_fresh_server_name_and_hidden_file_is_not_assignable(self):
        # Another user created it after this screen cached an empty group list.
        self.groups = {GROUP: {'group_id': GROUP, 'name': '공유 그룹', 'version': 1}}
        self.click(key='row_group_assign_' + SOURCE)
        self.widget('text_input', key='assign_group_name').set_value('공유 그룹')
        self.click('지정')
        self.assertEqual(len(self.groups), 1)
        self.assertEqual(self.memberships, {(GROUP, SOURCE)})
        self.deleted.add(('source-documents', SOURCE))
        self.widget('checkbox', key='include_deleted_source').check().run()
        self.click(key='query_source')
        self.assertTrue(self.widget('button', key='row_group_assign_' + SOURCE).disabled)
        self.assertFalse(self.widget('button', key='row_group_remove_' + SOURCE).disabled)

    def test_group_navigation_restarts_cursor_and_lookup_failure_does_not_widen_filter(self):
        self.groups = {GROUP: {'group_id': GROUP, 'name': 'A', 'version': 1}}
        self.memberships = {(GROUP, SOURCE), (GROUP, SOURCE2)}
        self.paginate = True
        self.app = self.new_app(query_ocr=False)
        self.click(key='next_source')
        self.widget('selectbox', key='source_group_filter').set_value(GROUP).run(); self.clean()
        last = [c for c in self.calls if c[:2] == ('LIST', 'source-documents')][-1]
        self.assertEqual(last[2]['group_id'], GROUP)
        self.assertEqual(last[2].get('cursor', ''), '')
        self.group_error = data_api.APIError('offline', 503)
        self.click(key='reload_groups')
        self.click(key='query_source')
        last = [c for c in self.calls if c[:2] == ('LIST', 'source-documents')][-1]
        self.assertEqual(last[2]['group_id'], GROUP)
        self.assertFalse([c for c in self.calls if c[0].startswith('GROUP_')])

    def test_navigation_search_pagination_and_rerun_are_read_only(self):
        self.paginate = True
        self.page('source')
        self.widget('text_input', key='q_source').set_value('검색어')
        self.click('조회 / 새로고침')
        self.widget('text_input', key='q_source').set_value('아직 조회하지 않은 값')
        self.click(key='next_source')
        last = [c for c in self.calls if c[:2] == ('LIST', 'source-documents')][-1]
        self.assertEqual(last[2]['q'], '검색어')
        self.assertEqual(last[2]['cursor'], 'next+page/=')
        self.click(key='prev_source')
        self.page('refine'); self.app.run(); self.clean()
        self.assertFalse(self.posts())

    def test_unified_list_shows_upload_and_web_files_with_row_actions_without_fetching_bytes(self):
        self.source_ids = [SOURCE, SOURCE2]
        self.source_payloads[SOURCE] = (pdf_fixture(), 'pdf')
        self.source_metadata[SOURCE] = {'original_filename': 'AI_Engineer_sample.pdf', 'source_url': '', 'source_type': 'upload'}
        self.app = self.new_app()
        self.assertEqual(self.widget('radio', key='data_nav').options, ['자료 목록', '결과 확인'])
        self.assertEqual(self.app.session_state.data_console['page'], 'source')
        self.assertEqual(self.widget('button', key='detail_' + SOURCE).label, r'AI\_Engineer\_sample\.pdf')
        self.assertTrue(any('example.com/attachment.html' in x.value for x in self.app.text))
        for id in self.source_ids:
            self.assertFalse(self.widget('button', key='row_refine_' + id).disabled)
            self.assertTrue(self.widget('button', key='detail_' + id))
        self.assertEqual(len(self.app.get('download_button')), 2)
        self.assertTrue(all(x.proto.deferred_file_id for x in self.app.get('download_button')))
        self.assertFalse(next(x for x in self.app.expander if x.label == '웹 가져오기 내역').proto.expanded)
        self.assertFalse([x for x in self.calls if x[0] == 'DOWNLOAD'])
        self.assertFalse(self.posts())

    def test_inventory_search_waits_for_query_and_name_opens_only_that_file(self):
        self.source_ids = [SOURCE, SOURCE2]
        self.app = self.new_app(query_ocr=False)
        before = len([c for c in self.calls if c[0] == 'LIST'])
        self.widget('text_input', key='q_source').set_value('보관 자료').run(); self.clean()
        self.assertEqual(len([c for c in self.calls if c[0] == 'LIST']), before)
        self.click(key='query_source')
        latest = [c for c in self.calls if c[:2] == ('LIST', 'source-documents')][-1]
        self.assertEqual(latest[2]['q'], '보관 자료')
        self.click(key='detail_' + SOURCE2)
        self.assertEqual(self.app.session_state.data_console['selected'], SOURCE2)
        self.assertTrue(any(c[:3] == ('GET', 'source-documents', SOURCE2) for c in self.calls))
        self.assertFalse(self.posts())

    def test_file_name_button_does_not_turn_markdown_into_remote_image(self):
        self.source_payloads[SOURCE] = (b'hello', 'text')
        self.source_metadata[SOURCE] = {'original_filename': '![표](https://example.com/pixel).pdf'}
        self.app = self.new_app(query_ocr=False)
        self.assertEqual(self.widget('button', key='detail_' + SOURCE).label,
                         r'\!\[표\]\(https://example\.com/pixel\)\.pdf')
        self.assertFalse([c for c in self.calls if c[0] in ('POST', 'DOWNLOAD')])

    def test_permanent_delete_needs_explicit_choice_and_acknowledgement(self):
        self.click(key='row_delete_source_' + SOURCE)
        self.widget('selectbox', key='delete_mode').set_value('파일까지 영구 삭제').run()
        self.assertTrue(self.widget('button', key='confirm_delete').disabled)
        self.assertFalse([c for c in self.calls if c[0] == 'PURGE'])
        self.widget('checkbox', key='confirm_purge_scope').check().run()
        self.click(key='confirm_delete')
        self.assertEqual(self.purged, {('source-documents', SOURCE)})
        self.assertFalse([c for c in self.calls if c[0] == 'DELETE'])

    def test_hidden_sources_can_be_purged_and_reference_rejection_is_recoverable(self):
        self.deleted.add(('source-documents', SOURCE))
        self.click(key='query_source')
        self.assertFalse([b for b in self.app.button if b.key == 'row_delete_source_' + SOURCE])
        self.widget('checkbox', key='include_deleted_source').check().run()
        self.click(key='query_source')
        self.click(key='row_delete_source_' + SOURCE)
        self.widget('selectbox', key='delete_mode').set_value('파일까지 영구 삭제').run()
        self.widget('checkbox', key='confirm_purge_scope').check().run()
        self.delete_error = data_api.APIError('SOURCE_HAS_RESULTS: results remain', 409, 'FailedPrecondition')
        self.click(key='confirm_delete')
        self.assertFalse(self.purged)
        self.assertTrue(any('정리 결과가 남아' in i.value for i in self.app.info))
        self.delete_error = None
        self.delete_lost = True
        self.click(key='confirm_delete')
        self.app.run(); self.clean()
        self.assertEqual(len([c for c in self.calls if c[0] == 'PURGE']), 2)
        self.delete_lost = False
        self.click(key='confirm_delete')
        self.assertEqual([c for c in self.calls if c[0] == 'PURGE'], [('PURGE', 'source-documents', SOURCE)] * 3)

    def test_source_delete_requires_confirmation_and_preserves_other_rows_and_filters(self):
        self.source_ids = [SOURCE, SOURCE2]
        self.app = self.new_app()
        self.widget('text_input', key='q_source').set_value('검색 조건')
        self.click(key='query_source')
        self.click(key='row_delete_source_' + SOURCE2)
        self.assertFalse([c for c in self.calls if c[0] == 'DELETE'])
        self.click(key='cancel_delete')
        self.assertFalse(self.deleted)
        self.click(key='row_delete_source_' + SOURCE2)
        self.click(key='confirm_delete')
        self.assertEqual(self.deleted, {('source-documents', SOURCE2)})
        self.assertEqual([c for c in self.calls if c[0] == 'DELETE'], [('DELETE', 'source-documents', SOURCE2)])
        self.assertFalse([b for b in self.app.button if b.key == 'detail_' + SOURCE2])
        self.assertTrue(self.widget('button', key='detail_' + SOURCE))
        latest = [c for c in self.calls if c[:2] == ('LIST', 'source-documents')][-1]
        self.assertEqual(latest[2]['q'], '검색 조건')
        self.assertEqual(latest[2]['cursor'], '')
        self.assertFalse(self.posts())

    def test_lost_delete_reply_never_repeats_automatically_and_retries_same_target(self):
        self.delete_lost = True
        self.click(key='row_delete_source_' + SOURCE)
        self.click(key='confirm_delete')
        self.assertIn(('source-documents', SOURCE), self.deleted)
        self.app.run(); self.clean()
        self.assertEqual(len([c for c in self.calls if c[0] == 'DELETE']), 1)
        self.assertEqual(self.app.session_state.data_console['deleting']['id'], SOURCE)
        self.delete_lost = False
        self.click(key='confirm_delete')
        self.assertEqual([c for c in self.calls if c[0] == 'DELETE'], [('DELETE', 'source-documents', SOURCE)] * 2)
        self.assertFalse(self.app.session_state.data_console.get('deleting'))
        self.assertFalse(self.posts())

    def test_result_delete_checks_live_state_and_keeps_failure_for_retry(self):
        self.running = True
        self.page('refine')
        self.click(key='row_delete_refine_' + REFINE)
        self.assertTrue(self.widget('button', key='confirm_delete').disabled)
        self.assertFalse([c for c in self.calls if c[0] == 'DELETE'])
        self.click(key='cancel_delete')
        self.running = False
        self.click(key='row_delete_refine_' + REFINE)
        self.delete_error = data_api.APIError('unavailable', 503, 'Unavailable')
        self.click(key='confirm_delete')
        self.assertFalse(self.deleted)
        self.assertEqual(self.app.session_state.data_console['deleting']['id'], REFINE)
        self.delete_error = None
        self.click(key='confirm_delete')
        self.assertEqual(self.deleted, {('refinement-runs', REFINE)})
        self.page('source')
        self.assertTrue(self.widget('button', key='detail_' + SOURCE))
        self.assertFalse(self.posts())

    def test_deleted_detail_keeps_original_and_result_but_blocks_new_refinement(self):
        self.deleted.add(('source-documents', SOURCE))
        self.deleted.add(('refinement-runs', REFINE))
        self.app = self.new_app({'page': 'source', 'id': SOURCE})
        self.assertTrue(self.widget('button', key='refine_this_source').disabled)
        self.assertEqual(self.app.session_state.data_console['artifact']['raw'], b'dom')
        self.assertTrue(any('목록에서 삭제한 자료' in n.value for n in self.app.info))
        self.app = self.new_app({'page': 'refine', 'id': REFINE})
        self.assertEqual(self.app.session_state.data_console['artifact']['raw'], self.raw)
        self.assertTrue(any('결과 목록에서 삭제한 작업' in n.value for n in self.app.info))
        self.assertFalse(self.posts())

    def test_delete_cannot_cross_gateway_or_pending_request(self):
        self.click(key='row_delete_source_' + SOURCE)
        target = self.app.session_state.data_console['deleting']
        target['gateway'] = 'http://different.example'
        self.app.run(); self.clean()
        self.assertTrue(self.widget('button', key='confirm_delete').disabled)
        self.assertFalse([c for c in self.calls if c[0] == 'DELETE'])
        self.click(key='cancel_delete')
        self.app.session_state.data_console['invalid_ticket'] = 'invalid request'
        self.app.run(); self.clean()
        self.assertTrue(self.widget('button', key='row_delete_source_' + SOURCE).disabled)

    def test_row_downloads_freeze_each_file_preserve_upload_bytes_and_reject_corruption(self):
        import streamlit as st
        import data_ui_documents
        from concurrent.futures import ThreadPoolExecutor
        captures = {}
        download = st.download_button
        def capture(label, data, **kwargs):
            if kwargs.get('key', '').startswith('row_download_'): captures[kwargs['key']] = (data, kwargs)
            return download(label, data, **kwargs)
        pdf = pdf_fixture()
        self.source_ids = [SOURCE, SOURCE2]
        self.source_payloads.update({SOURCE: (pdf, 'pdf'), SOURCE2: (b'<p>web original</p>', 'html')})
        self.source_metadata[SOURCE] = {'original_filename': 'AI_Engineer_sample.pdf', 'source_url': '', 'source_type': 'upload'}
        with patch.object(st, 'download_button', side_effect=capture):
            self.app = self.new_app()
        first, options = captures['row_download_' + SOURCE]
        second, _ = captures['row_download_' + SOURCE2]
        self.assertEqual(options['file_name'], 'AI_Engineer_sample.pdf')
        self.assertEqual(options['on_click'], 'ignore')
        self.assertFalse([x for x in self.calls if x[0] == 'DOWNLOAD'])
        # Change the displayed list after rendering. Deferred downloads must keep
        # each original identity and never borrow the currently selected row.
        item = self.app.session_state.data_console['lists']['source']['data']['items'][0]
        item.update(document_id=SOURCE2, sha256='0' * 64, size_bytes=1)
        with patch.object(data_ui_documents, 'API', wraps=data_ui_documents.API) as factory:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(f) for f in (first, second)]
                self.assertEqual([f.result() for f in futures], [pdf, b'<p>web original</p>'])
            self.assertEqual([c.args for c in factory.call_args_list], [(BASE,), (BASE,)])
        self.corrupt = True
        with self.assertRaises(RuntimeError): first()
        self.corrupt = False
        self.assertEqual(first(), pdf)
        self.assertFalse(self.posts())

    def test_row_refinement_submits_only_clicked_document_and_preserves_lost_reply(self):
        self.source_ids = [SOURCE, SOURCE2]
        self.app = self.new_app()
        self.lost = True
        self.click(key='row_refine_' + SOURCE2)
        first = self.posts()[0]
        self.assertEqual(first[2]['document_id'], SOURCE2)
        self.assertTrue(self.widget('button', key='row_refine_' + SOURCE).disabled)
        self.assertTrue(self.widget('button', key='row_refine_' + SOURCE2).disabled)
        self.app.run(); self.clean()
        self.assertEqual(len(self.posts()), 1)
        self.lost = False
        self.click(key='retry_pending')
        self.assertEqual(self.posts()[1], first)
        self.assertEqual(self.app.session_state.data_console['selected'], REFINE)

    def test_row_pdf_gate_does_not_block_html_or_original_download(self):
        self.source_ids = [SOURCE, SOURCE2]
        self.source_payloads[SOURCE] = (pdf_fixture(), 'pdf')
        self.deployment_state = dict(self.deployment_state, runtime_state='stopped')
        self.app = self.new_app()
        self.assertFalse(self.widget('button', key='row_refine_' + SOURCE).disabled)
        self.assertFalse(self.widget('button', key='row_refine_' + SOURCE2).disabled)
        self.assertEqual(len(self.app.get('download_button')), 2)
        self.assertFalse(any(x.proto.disabled for x in self.app.get('download_button')))
        self.click(key='row_refine_' + SOURCE2)
        self.assertNotIn('ocr_deployment_id', self.posts()[0][2])

    def test_row_pdf_rechecks_deployment_before_post(self):
        self.source_payloads[SOURCE] = (pdf_fixture(), 'pdf')
        self.app = self.new_app()
        self.deployment_state = dict(self.deployment_state, desired_state='stopped')
        self.click(key='row_refine_' + SOURCE)
        self.assertEqual(len(self.posts()), 1)
        self.assertNotIn('ocr_deployment_id', self.posts()[0][2])
        self.assertIsNone(self.app.session_state.data_console['pending'])
        self.assertEqual(self.app.session_state.data_console['selected'], REFINE)

    def test_legacy_list_urls_resolve_to_unified_list_and_crawl_detail_returns_there(self):
        for page in ('crawl', 'source'):
            self.app = self.new_app({'page': page})
            self.assertEqual(self.app.session_state.data_console['page'], 'source')
            self.assertTrue(self.widget('button', key='row_refine_' + SOURCE))
        self.app = self.new_app({'page': 'crawl', 'id': RUN, 'file': SOURCE2})
        self.assertEqual(self.widget('radio', key='data_nav').value, 'source')
        self.assertEqual(self.app.session_state.data_console['preview_file'], SOURCE2)
        self.click(key='back_results')
        self.assertEqual(self.app.query_params['page'], ['source'])
        self.assertNotIn('id', self.app.query_params)
        self.assertTrue(self.widget('button', key='row_refine_' + SOURCE))
        self.assertFalse(self.posts())

    def test_collection_completion_invalidates_stale_materials_list(self):
        self.source_ids = []
        self.app = self.new_app()
        self.assertFalse([x for x in self.app.button if str(x.key).startswith('row_refine_')])
        self.running = True
        self.click(key='detail_' + RUN)
        self.source_ids = [SOURCE, SOURCE2]
        self.running = False
        self.click(key='refresh_collection')
        self.click(key='back_results')
        for id in self.source_ids: self.assertTrue(self.widget('button', key='row_refine_' + id))
        self.assertFalse(self.posts())

    def test_lost_reply_retries_original_key_and_never_auto_resubmits(self):
        self.lost = True
        self.crawl_form(); self.click('가져오기')
        key = self.posts()[0][3]
        self.assertIn('pending', self.app.query_params)
        self.app.run(); self.clean()
        self.assertEqual(len(self.posts()), 1)
        self.assertFalse([x for x in self.app.text_input if x.key == 'name_crawl'])
        self.click(key='close_request')
        self.lost = False
        self.click(key='retry_pending')
        self.assertEqual([x[3] for x in self.posts()], [key, key])
        self.assertNotIn('pending', self.app.query_params)
        self.assertEqual(self.app.session_state.data_console['selected'], RUN)

    def test_new_browser_session_restores_request_without_reentry(self):
        self.lost = True
        self.crawl_form(); self.click('가져오기')
        saved = self.app.query_params['pending'][0]
        original = self.posts()[0]
        self.app = self.new_app({'pending': saved})
        self.assertEqual(len(self.posts()), 1)
        self.assertTrue(self.widget('button', '자료 가져오기').disabled)
        self.lost = False
        self.click(key='retry_pending')
        self.assertEqual(self.posts()[1][2:], original[2:])

    def test_crawl_needs_only_url_and_validation_does_not_lock_form(self):
        self.click('자료 가져오기')
        self.click('가져오기')
        self.assertFalse(self.posts())
        self.assertIsNone(self.app.session_state.data_console['pending'])
        self.widget('text_input', key='crawl_url').set_value('https://example.com/reports')
        self.click('가져오기')
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.posts()[0][2]['name'], 'https://example.com/reports 수집')

    def test_old_pending_link_can_start_a_new_request_without_matching_inputs(self):
        t = data_state.ticket('crawl', {'name': 'old', 'url': 'https://old.example', 'options': {}}, BASE)
        self.app = self.new_app({'pending': data_state.pack(t)})
        self.assertFalse(self.posts())
        self.click(key='new_retry_pending')
        self.widget('text_input', key='crawl_url').set_value('https://new.example')
        self.click('가져오기')
        self.assertEqual(len(self.posts()), 1)
        self.assertNotEqual(self.posts()[0][3], t['key'])
        self.assertEqual(self.posts()[0][2]['url'], 'https://new.example')

    def test_known_acceptance_from_old_link_opens_result_without_post(self):
        self.lost = True
        self.crawl_form(); self.click('가져오기')
        saved = self.app.query_params['pending'][0]
        self.lost = False
        self.click(key='retry_request')
        self.assertEqual(len(self.posts()), 2)
        self.app = self.new_app({'pending': saved})
        self.assertEqual(len(self.posts()), 2)
        self.click('실행 결과 보기')
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(self.app.session_state.data_console['selected'], RUN)

    def test_upload_retry_in_new_session_keeps_file_without_selecting_again(self):
        import streamlit as st
        class Upload:
            name = '한글.txt'
            def getvalue(self): return b'\x00original\xff'
        self.lost = True
        self.page('source')
        with patch.object(st, 'file_uploader', return_value=Upload()):
            self.upload_form(); self.click('가져오기')
        saved = self.app.query_params['pending'][0]
        original = self.posts()[0]
        self.assertEqual(original[2]['name'], '한글.txt')
        self.app = self.new_app({'pending': saved})
        self.assertEqual(len(self.posts()), 1)
        self.lost = False
        self.click(key='retry_pending')
        self.assertEqual(self.posts()[1][2:], original[2:])

    def test_request_storage_failure_does_not_send_unrecoverable_post(self):
        import data_ui_actions
        from data_requests import RequestStoreError
        self.crawl_form()
        with patch.object(data_ui_actions.RequestStore, 'prepare', side_effect=RequestStoreError('저장 실패')):
            self.click('가져오기')
        self.assertFalse(self.posts())
        original_key = self.app.session_state.data_console['pending']['key']
        self.click(key='retry_request')
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.posts()[0][3], original_key)

    def test_direct_refine_lost_reply_recovers_with_original_file_and_key(self):
        self.click(key='detail_' + RUN)
        self.choose_file(SOURCE2)
        self.lost = True
        self.click('내용 정리하기')
        original = self.posts()[0]
        self.assertEqual(original[2]['document_id'], SOURCE2)
        self.app.run(); self.clean()
        self.assertEqual(len(self.posts()), 1)
        self.lost = False
        self.click(key='retry_pending')
        self.assertEqual(self.posts()[1][2:], original[2:])

    def test_html_refinement_does_not_send_ocr_even_when_deployment_is_configured(self):
        self.click(key='detail_' + RUN)
        self.click('내용 정리하기')
        self.assertEqual(len(self.posts()), 1)
        self.assertNotIn('ocr_deployment_id', self.posts()[0][2])
        self.assertEqual(self.posts()[0][2]['document_id'], SOURCE)
        self.assertEqual(self.app.session_state.data_console['selected'], REFINE)

    def test_invalid_ticket_blocks_mutations_but_allows_lists(self):
        self.app = self.new_app({'pending': 'broken'})
        self.assertTrue(self.widget('button', '자료 가져오기').disabled)
        self.page('source')
        self.assertTrue(self.widget('button', key='row_refine_' + SOURCE).disabled)
        self.assertFalse(self.posts())

    def test_browser_crawl_options_and_source_links(self):
        self.crawl_form()
        self.widget('checkbox', key='crawl_attachments').set_value(True)
        self.click('가져오기')
        body = self.posts()[0][2]
        self.assertTrue(body['options']['render_javascript'])
        self.assertTrue(body['options']['include_attachments'])
        self.assertEqual(body['options']['render_wait_ms'], 1000)
        self.choose_file(SOURCE)
        self.assertEqual(self.app.session_state.data_console['page'], 'crawl')
        self.assertEqual(self.app.session_state.data_console['preview_file'], SOURCE)

    def test_url_results_keep_files_and_preview_together_without_new_jobs(self):
        self.click(key='detail_' + RUN)
        self.assertTrue(any('https://example.com/reports 수집 결과' in str(x.proto) for x in self.app.get('html')))
        self.assertIn('가져온 자료', [x.value for x in self.app.subheader])
        self.assertTrue(any('첨부파일' in x for x in self.widget('radio', key='collection_file_' + RUN).options))
        self.assertTrue(any('웹페이지 · HTML' in x.value for x in self.app.caption))
        self.assertEqual([x[2] for x in self.calls if x[0] == 'DOWNLOAD'], [SOURCE])
        self.choose_file(SOURCE2)
        self.assertEqual(self.app.query_params['id'], [RUN])
        self.assertEqual(self.app.query_params['file'], [SOURCE2])
        self.assertEqual(self.app.session_state.data_console['page'], 'crawl')
        self.assertEqual([x[2] for x in self.calls if x[0] == 'DOWNLOAD'], [SOURCE, SOURCE2])
        self.app.run(); self.clean()
        self.click(key='refresh_collection')
        self.assertEqual([x[2] for x in self.calls if x[0] == 'DOWNLOAD'], [SOURCE, SOURCE2])
        self.assertFalse(self.posts())
        self.click(key='back_results')
        self.assertNotIn('id', self.app.query_params)

    def test_result_link_restores_only_a_file_belonging_to_its_collection(self):
        self.app = self.new_app({'page': 'crawl', 'id': RUN, 'file': SOURCE2})
        self.assertEqual(self.app.session_state.data_console['preview_file'], SOURCE2)
        self.assertEqual([x[2] for x in self.calls if x[0] == 'DOWNLOAD'], [SOURCE2])
        self.calls.clear()
        self.app = self.new_app({'page': 'crawl', 'id': RUN, 'file': CANON})
        self.assertEqual(self.app.session_state.data_console['preview_file'], SOURCE)
        self.assertNotIn(('GET', 'source-documents', CANON), self.calls)
        self.assertFalse(self.posts())

    def test_one_file_metadata_failure_keeps_other_files_usable(self):
        self.missing_source = SOURCE
        self.click(key='detail_' + RUN)
        self.assertIn('파일 정보를 불러오지 못했습니다.', [x.value for x in self.app.warning])
        self.assertEqual(self.app.session_state.data_console['preview_file'], SOURCE2)
        self.assertEqual(len(self.app.get('download_button')), 1)
        self.missing_source = ''
        self.click(key='refresh_collection')
        self.choose_file(SOURCE)
        self.assertEqual(self.app.session_state.data_console['preview_file'], SOURCE)
        self.assertFalse(self.posts())

    def test_running_collection_waits_for_real_files_then_finishes(self):
        self.running = True
        self.click(key='detail_' + RUN)
        self.assertFalse([x for x in self.calls if x[0] == 'DOWNLOAD'])
        self.assertTrue(any('수집된 파일 0개' in x.value for x in self.app.text))
        self.running = False
        self.click(key='refresh_collection')
        self.assertTrue(self.app.session_state.data_console['settled'])
        self.assertEqual(self.app.session_state.data_console['preview_file'], SOURCE)
        self.assertFalse(self.posts())

    def test_refine_selected_collection_file_uses_that_file_id(self):
        self.click(key='detail_' + RUN)
        self.choose_file(SOURCE2)
        self.click('내용 정리하기')
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.posts()[0][2]['document_id'], SOURCE2)

    def test_failed_preview_retains_verified_download(self):
        import data_ui_documents
        from data_preview import PreviewError
        with patch.object(data_ui_documents, 'preview', side_effect=PreviewError('미리보기 처리 실패')):
            self.click(key='detail_' + RUN)
        self.assertTrue(any('미리보기를 만들 수 없습니다' in x.value for x in self.app.info))
        self.assertEqual(len(self.app.get('download_button')), 1)
        self.assertEqual(self.app.session_state.data_console['artifact']['raw'], b'dom')
        self.assertFalse(self.posts())

    def test_pdf_page_selection_updates_preview_without_downloading_source_again(self):
        self.source_payloads[SOURCE] = (pdf_fixture(), 'pdf')
        self.click(key='detail_' + RUN)
        first = self.app.session_state.data_console['preview_cache']['result']['png']
        self.widget('number_input', '페이지').set_value(2).run(); self.clean()
        result = self.app.session_state.data_console['preview_cache']['result']
        self.assertEqual(result['page'], 1)
        self.assertNotEqual(result['png'], first)
        self.assertEqual(len([x for x in self.calls if x[0] == 'DOWNLOAD']), 1)
        self.assertFalse(self.posts())

    def test_spreadsheet_selection_shows_selected_sheet(self):
        self.source_payloads[SOURCE] = (workbook(), 'xlsx')
        self.click(key='detail_' + RUN)
        self.assertEqual(self.app.dataframe[0].value.iloc[0]['A'], '한글 제목')
        self.widget('number_input', '시트 번호').set_value(2).run(); self.clean()
        self.assertEqual(self.app.dataframe[0].value.iloc[0]['A'], '둘째 시트')
        self.assertEqual(len([x for x in self.calls if x[0] == 'DOWNLOAD']), 1)
        self.assertFalse(self.posts())

    def test_collection_chooses_browser_and_bounded_scope_without_engine_inputs(self):
        self.crawl_form(); self.click('가져오기')
        options = self.posts()[0][2]['options']
        self.assertTrue(options['render_javascript'])
        self.assertTrue(options['include_attachments'])
        self.assertFalse(options['follow_links'])
        self.assertEqual(options['max_depth'], 0)
        self.assertEqual(options['max_documents'], 10)

    def test_result_page_selects_documents_without_id_entry(self):
        self.page('refine'); self.click('정리할 자료 선택')
        self.assertEqual(self.app.session_state.data_console['page'], 'source')
        self.assertFalse(self.posts())
        self.click(key='detail_' + SOURCE)
        self.click('내용 정리하기')
        self.assertEqual(self.posts()[0][2]['document_id'], SOURCE)

    def test_two_user_menus_and_forms_have_no_technical_controls(self):
        self.assertEqual(self.widget('radio', key='data_nav').options, ['자료 목록', '결과 확인'])
        self.crawl_form()
        self.assertEqual([w.label for w in self.app.text_input if w.key == 'crawl_url'], ['웹페이지 주소'])
        self.assertFalse([w for w in self.app.text_input if str(w.key).startswith('name_')])
        labels = [w.label for kind in ('text_input', 'checkbox', 'number_input', 'button', 'selectbox') for w in getattr(self.app, kind)]
        for term in ['JavaScript', 'ID', 'CPU', '렌더링', 'MiB']:
            self.assertFalse(any(term in label for label in labels), (term, labels))
        self.assertFalse(self.posts())

    def test_image_and_pdf_refinement_use_configured_recognition_automatically(self):
        from PIL import Image
        for kind in ('pdf', 'png', 'jpeg'):
            with self.subTest(kind=kind):
                stream = io.BytesIO()
                Image.new('RGB', (12, 12), 'white').save(stream, 'JPEG' if kind == 'jpeg' else 'PNG')
                self.source_payloads[SOURCE] = (pdf_fixture() if kind == 'pdf' else stream.getvalue(), kind)
                self.app = self.new_app({'page': 'source', 'id': SOURCE})
                before = len(self.posts())
                self.click('내용 정리하기')
                self.assertEqual(len(self.posts()), before + 1)
                self.assertEqual(self.posts()[-1][2]['ocr_deployment_id'], DEP)
                self.assertEqual(self.app.session_state.data_console['selected'], REFINE)

    def test_document_formats_run_without_recognition_settings(self):
        from test_data_preview import package
        fixtures = {'text': b'hello', 'markdown': b'# Hello', 'html': b'<p>Hello</p>',
                    'docx': package({'word/document.xml': '<document><body><p><t>Hello</t></p></body></document>'}),
                    'xlsx': workbook()}
        for kind, raw in fixtures.items():
            with self.subTest(kind=kind):
                self.source_payloads[SOURCE] = (raw, kind)
                self.app = self.new_app({'page': 'source', 'id': SOURCE})
                self.click('내용 정리하기')
                self.assertNotIn('ocr_deployment_id', self.posts()[-1][2])
                self.assertEqual(self.app.session_state.data_console['selected'], REFINE)

    def test_recognition_status_is_get_only_cached_and_explicitly_refreshable(self):
        self.assertEqual(self.widget('selectbox', key='ocr_model_choice').value, DEP)
        self.assertFalse(next(x for x in self.app.expander if x.label == '글자 인식 연결 정보').proto.expanded)
        reads = lambda: len([x for x in self.calls if x[0].startswith('DEPLOYMENT_')])
        before = reads()
        self.app.run(); self.clean()
        self.assertEqual(reads(), before)
        self.deployment_state = dict(self.deployment_state, desired_state='stopped')
        self.click(key='query_ocr_models')
        self.assertEqual(reads(), before + 1)
        self.assertTrue(any('사용 가능한 OCR 모델이 없어 파서만 사용합니다' in x.value for x in self.app.info))
        self.assertFalse(self.posts())

    def test_ocr_lookup_requires_click_and_excludes_chat_deployments(self):
        self.calls.clear()
        self.app = self.new_app(query_ocr=False)
        self.assertFalse([x for x in self.calls if x[0].startswith('DEPLOYMENT_')])
        self.assertFalse([x for x in self.app.selectbox if x.key == 'ocr_model_choice'])
        self.deployment_pages = {'': {'items': [self.deployment_state,
            dict(self.deployment_state, deployment_id=SOURCE2, serving={'engine': 'vllm', 'profile': ''})], 'next_cursor': ''}}
        self.click(key='query_ocr_models')
        self.assertEqual(self.widget('selectbox', key='ocr_model_choice').options, ['모델을 선택하세요', 'Surya OCR · ' + DEP[:8]])
        self.app.run(); self.clean()
        self.assertEqual(len([x for x in self.calls if x[0] == 'DEPLOYMENT_LIST']), 1)
        self.assertFalse(self.posts())

    def test_selected_ocr_model_is_used_and_pending_request_keeps_its_original_model(self):
        other = dict(self.deployment_state, deployment_id=SOURCE2)
        self.deployment_pages = {'': {'items': [self.deployment_state, other], 'next_cursor': ''}}
        self.source_payloads[SOURCE] = (pdf_fixture(), 'pdf')
        self.app = self.new_app()
        self.widget('selectbox', key='ocr_model_choice').set_value(SOURCE2).run(); self.clean()
        self.assertFalse(self.posts())
        self.deployment_state = other
        self.lost = True
        self.click(key='row_refine_' + SOURCE)
        first = self.posts()[0]
        self.assertEqual(first[2]['ocr_deployment_id'], SOURCE2)
        self.widget('selectbox', key='ocr_model_choice').set_value(DEP).run(); self.clean()
        self.assertEqual(len(self.posts()), 1)
        self.lost = False
        self.click(key='retry_pending')
        self.assertEqual(self.posts()[1], first)

    def test_no_config_still_allows_explicit_ocr_selection_and_empty_page_can_continue(self):
        with patch.dict(os.environ, {'DATA_UI_OCR_DEPLOYMENT_ID': ''}):
            self.deployment_pages = {'': {'items': [dict(self.deployment_state, serving={'engine': 'vllm', 'profile': ''})], 'next_cursor': 'page2'},
                                     'page2': {'items': [self.deployment_state], 'next_cursor': ''}}
            self.source_payloads[SOURCE] = (pdf_fixture(), 'pdf')
            self.app = self.new_app()
            self.assertFalse(self.widget('button', key='row_refine_' + SOURCE).disabled)
            self.click(key='next_ocr_models')
            self.assertEqual(self.widget('selectbox', key='ocr_model_choice').value, '')
            self.widget('selectbox', key='ocr_model_choice').set_value(DEP).run(); self.clean()
            self.assertFalse(self.widget('button', key='row_refine_' + SOURCE).disabled)
            self.assertFalse(self.posts())

    def test_failed_refresh_or_changed_server_clears_old_selection(self):
        self.source_payloads[SOURCE] = (pdf_fixture(), 'pdf')
        self.app = self.new_app()
        self.deployment_state = data_api.APIError('offline', code='CONNECTION_ERROR')
        self.click(key='query_ocr_models')
        self.assertEqual(self.app.session_state.data_console['ocr_selection'], '')
        self.assertFalse(self.widget('button', key='row_refine_' + SOURCE).disabled)
        self.assertTrue(self.app.error)
        with patch.dict(os.environ, {'MODEL_GATEWAY_URL': 'http://another:8000'}):
            self.app.run(); self.clean()
            self.assertIsNone(self.app.session_state.data_console['ocr_catalog'])
        self.assertFalse(self.posts())

    def test_stopped_selected_model_is_not_replaced_by_another_after_refresh(self):
        self.source_payloads[SOURCE] = (pdf_fixture(), 'pdf')
        self.deployment_pages = {'': {'items': [dict(self.deployment_state, deployment_id=SOURCE2)], 'next_cursor': ''}}
        self.click(key='query_ocr_models')
        self.assertEqual(self.widget('selectbox', key='ocr_model_choice').value, '')
        self.assertFalse(self.posts())

    def test_repeating_catalog_cursor_fails_without_silent_partial_success(self):
        self.deployment_pages = {'': {'items': [], 'next_cursor': 'repeat'}, 'repeat': {'items': [self.deployment_state], 'next_cursor': 'repeat'}}
        self.click(key='query_ocr_models')
        self.click(key='next_ocr_models')
        self.assertTrue(self.app.error)
        self.assertEqual(self.app.session_state.data_console['ocr_selection'], '')
        self.assertFalse(self.posts())

    def test_pdf_start_rechecks_selected_model_and_falls_back_before_ticket(self):
        self.source_payloads[SOURCE] = (pdf_fixture(), 'pdf')
        self.app = self.new_app({'page': 'source', 'id': SOURCE})
        self.deployment_state = dict(self.deployment_state, runtime_state='stopped')
        self.click('내용 정리하기')
        self.assertEqual(len(self.posts()), 1)
        self.assertNotIn('ocr_deployment_id', self.posts()[0][2])
        self.assertIsNone(self.app.session_state.data_console['pending'])

    def test_unavailable_or_unselected_recognition_uses_parser_and_preserves_downloads(self):
        ready = dict(self.deployment_state)
        cases = [dict(ready, status='applying'), dict(ready, runtime_state='unknown'),
                 dict(ready, desired_state='stopped'), dict(ready, artifact_kind='adapter'),
                 dict(ready, deployment_id=SOURCE), dict(ready, serving={'engine': 'vllm', 'profile': 'chat'}),
                 dict(ready, serving=None), data_api.APIError('missing', status=404),
                 data_api.APIError('timeout', code='CONNECTION_ERROR')]
        self.source_payloads[SOURCE] = (pdf_fixture(), 'pdf')
        for value in cases:
            with self.subTest(state=str(value)):
                self.deployment_state = value
                self.app = self.new_app({'page': 'source', 'id': SOURCE})
                self.assertFalse(self.widget('button', key='refine_this_source').disabled)
                self.assertTrue(self.app.get('download_button'))
                before=len(self.posts())
                self.click('내용 정리하기')
                self.assertEqual(len(self.posts()),before+1)
                self.assertNotIn('ocr_deployment_id',self.posts()[-1][2])
                self.app = self.new_app({'page': 'refine', 'id': REFINE})
                self.assertTrue(self.app.get('download_button'))
        for deployment in ('', 'not-a-uuid'):
            with patch.dict(os.environ, {'DATA_UI_OCR_DEPLOYMENT_ID': deployment}):
                self.app = self.new_app({'page': 'source', 'id': SOURCE})
                self.assertFalse(self.widget('button', key='refine_this_source').disabled)
                self.click('내용 정리하기')
                self.assertNotIn('ocr_deployment_id',self.posts()[-1][2])

    def test_fast_option_omits_ocr_and_skips_deployment_recheck(self):
        self.source_payloads[SOURCE] = (pdf_fixture(), 'pdf')
        self.app = self.new_app({'page':'source','id':SOURCE})
        self.assertFalse(self.widget('toggle',key='fast_work').value)
        self.widget('toggle',key='fast_work').set_value(True).run();self.clean()
        before=len([c for c in self.calls if c[0].startswith('DEPLOYMENT_')])
        self.click('내용 정리하기')
        self.assertNotIn('ocr_deployment_id',self.posts()[-1][2])
        self.assertEqual(len([c for c in self.calls if c[0].startswith('DEPLOYMENT_')]),before)
        self.assertEqual(self.app.session_state.data_console['selected'], REFINE)

    def test_unqueried_model_list_does_not_block_parser_or_trigger_hidden_lookup(self):
        self.source_payloads[SOURCE]=(pdf_fixture(),'pdf')
        self.calls.clear()
        self.app=self.new_app({'page':'source','id':SOURCE},query_ocr=False)
        self.click('내용 정리하기')
        self.assertFalse([c for c in self.calls if c[0].startswith('DEPLOYMENT_')])
        self.assertNotIn('ocr_deployment_id',self.posts()[-1][2])

    def test_fast_pending_request_cannot_gain_ocr_when_toggle_changes(self):
        self.source_payloads[SOURCE]=(pdf_fixture(),'pdf')
        self.app=self.new_app({'page':'source','id':SOURCE})
        self.widget('toggle',key='fast_work').set_value(True).run();self.clean()
        self.lost=True
        self.click('내용 정리하기')
        original=self.posts()[-1]
        self.widget('toggle',key='fast_work').set_value(False).run();self.clean()
        self.lost=False
        self.click(key='retry_pending')
        self.assertEqual(self.posts()[-1],original)
        self.assertNotIn('ocr_deployment_id',original[2])

    def test_ocr_pending_request_cannot_lose_ocr_when_fast_enabled(self):
        self.source_payloads[SOURCE]=(pdf_fixture(),'pdf')
        self.app=self.new_app({'page':'source','id':SOURCE})
        self.lost=True
        self.click('내용 정리하기')
        original=self.posts()[-1]
        self.widget('toggle',key='fast_work').set_value(True).run();self.clean()
        self.lost=False
        self.click(key='retry_pending')
        self.assertEqual(self.posts()[-1],original)
        self.assertEqual(original[2]['ocr_deployment_id'],DEP)

    def test_text_can_be_refined_when_model_gateway_is_unavailable(self):
        self.deployment_state = data_api.APIError('timeout', code='CONNECTION_ERROR')
        self.source_payloads[SOURCE] = (b'hello', 'text')
        self.app = self.new_app({'page': 'source', 'id': SOURCE})
        self.click('내용 정리하기')
        self.assertNotIn('ocr_deployment_id', self.posts()[-1][2])

    def test_pending_ocr_reply_replays_original_identity_even_if_config_or_status_changes(self):
        self.source_payloads[SOURCE] = (pdf_fixture(), 'pdf')
        self.app = self.new_app({'page': 'source', 'id': SOURCE})
        self.lost = True
        self.click('내용 정리하기')
        original = self.posts()[-1]
        recovery_url = dict(self.app.query_params)
        self.deployment_state = data_api.APIError('offline', code='CONNECTION_ERROR')
        self.lost = False
        with patch.dict(os.environ, {'DATA_UI_OCR_DEPLOYMENT_ID': SOURCE2}):
            self.app = self.new_app(recovery_url)
            self.assertEqual(len(self.posts()), 1)
            self.click(key='retry_pending')
        self.assertEqual(self.posts()[-1], original)
        self.assertEqual(len(self.posts()), 2)

    def test_result_and_original_can_be_compared_without_new_requests(self):
        self.page('refine'); self.click(key='detail_' + REFINE)
        self.assertEqual(self.app.session_state.data_console['artifact']['raw'], self.raw)
        self.widget('radio', key='result_view_' + CANON).set_value('원본 자료').run(); self.clean()
        self.assertEqual(self.app.session_state.data_console['artifact']['raw'], b'dom')
        self.assertFalse([x for x in self.app.button if x.key == 'refine_this_source'])
        self.widget('radio', key='result_view_' + CANON).set_value('정리한 내용').run(); self.clean()
        self.assertEqual(self.app.session_state.data_console['artifact']['raw'], self.raw)
        self.assertFalse(self.posts())

    def test_main_flow_has_one_primary_action_and_optional_controls_stay_closed(self):
        def visible(node):
            yield node
            if node.type == 'popover' or (node.type == 'expander' and not node.proto.expanded):
                return
            for child in getattr(node, 'children', {}).values():
                yield from visible(child)
        def primary():
            found = []
            for node in visible(self.app.main):
                if node.type == 'button' and node.proto.type == 'primary': found.append(node.label)
                if node.type == 'popover' and node.proto.popover.type == 'primary': found.append(node.proto.popover.label)
            return found
        self.assertEqual(primary(), ['자료 가져오기'])
        self.assertTrue(all(x.disabled for x in self.app.button if str(x.key).startswith(('prev_', 'next_'))))
        self.page('source'); self.click(key='detail_' + SOURCE)
        self.assertEqual(primary(), ['내용 정리하기'])
        self.assertFalse([x for x in visible(self.app.main) if x.type == 'download_button'])
        self.page('refine'); self.click(key='detail_' + REFINE)
        self.assertEqual(primary(), ['결과 다운로드'])
        self.assertFalse([x for x in visible(self.app.main) if x.type in ('selectbox', 'download_button', 'checkbox') and getattr(x, 'key', '') != 'ocr_model_choice'])
        self.assertTrue(all(not x.proto.popover.open for x in self.app.get('popover')))
        self.assertFalse([x for x in self.app.checkbox if str(x.key).startswith('poll_')])
        self.assertFalse(self.posts())

    def test_download_selection_uses_complete_stored_result_without_new_work(self):
        import streamlit as st
        captures = []
        real_download = st.download_button
        def capture(label, data, **kwargs):
            captures.append((data, kwargs['file_name'], kwargs['mime']))
            return real_download(label, data, **kwargs)
        value = json.loads(self.raw)
        value['blocks'].extend({'type': 'paragraph', 'text': '본문' + str(i)} for i in range(25))
        self.raw = data_api.encode(value)
        with patch.object(st, 'download_button', side_effect=capture):
            self.page('refine'); self.click(key='detail_' + REFINE)
            self.assertTrue(captures[-1][1].endswith('.md'))
            self.assertIn('본문24', captures[-1][0].decode())
            self.widget('selectbox', key='result_format').set_value('전체 구조 (.json)').run(); self.clean()
            self.assertEqual(captures[-1], (self.raw, '정리한-결과.json', 'application/json'))
            self.widget('selectbox', key='result_format').set_value('텍스트 (.txt)').run(); self.clean()
            self.assertIn(b'24', captures[-1][0])
            self.assertEqual(captures[-1][1], '정리한-결과.txt')
        self.assertEqual(len([x for x in self.calls if x[0] == 'DOWNLOAD']), 1)
        self.assertFalse(self.posts())

    def test_fragmented_pdf_does_not_create_empty_preview_pages(self):
        value = json.loads(self.raw)
        value['blocks'] = [{'type': 'image', 'location': {'page': 3}} for _ in range(882)]
        value['blocks'].append({'type': 'text', 'text': '그림 뒤 본문', 'location': {'page': 3}})
        self.raw = data_api.encode(value)
        self.page('refine'); self.click(key='detail_' + REFINE)
        self.assertFalse([x for x in self.app.number_input if x.label == '내용 나눠 보기'])
        rendered = ''.join(x.proto.body for x in self.app.get('html'))
        self.assertIn('그림 뒤 본문', rendered)
        self.assertNotIn('[그림 영역', rendered)
        self.assertTrue(any('그림 영역 882개' in x.value for x in self.app.text))
        self.assertEqual(self.app.session_state.data_console['artifact']['raw'], self.raw)
        self.assertFalse(self.posts())

    def test_original_download_in_results_keeps_pdf_bytes_extension_and_canonical_cache(self):
        import streamlit as st
        captures = []
        download = st.download_button
        def capture(label, data, **kwargs):
            captures.append((label, data, kwargs['file_name']))
            return download(label, data, **kwargs)
        raw = pdf_fixture()
        self.source_payloads[SOURCE] = (raw, 'pdf')
        with patch.object(st, 'download_button', side_effect=capture):
            self.page('refine'); self.click(key='detail_' + REFINE)
            self.assertFalse([x for x in self.calls if x[:2] == ('DOWNLOAD', 'source-documents')])
            self.widget('selectbox', key='result_format').set_value('원본 파일').run(); self.clean()
            self.assertEqual(captures[-1], ('원본 다운로드', raw, 'source-' + SOURCE + '.pdf'))
            self.app.run(); self.clean()
            self.widget('selectbox', key='result_format').set_value('전체 구조 (.json)').run(); self.clean()
            self.assertEqual(captures[-1][1], self.raw)
        self.assertEqual(len([x for x in self.calls if x[:2] == ('DOWNLOAD', 'source-documents')]), 1)
        self.assertEqual(self.app.session_state.data_console['artifact']['raw'], self.raw)
        self.assertFalse(self.posts())

    def test_bad_original_download_can_retry_without_losing_result(self):
        self.page('refine'); self.click(key='detail_' + REFINE)
        self.corrupt = True
        self.widget('selectbox', key='result_format').set_value('원본 파일').run(); self.clean()
        self.assertFalse(self.app.get('download_button'))
        self.assertEqual(self.app.session_state.data_console['artifact']['raw'], self.raw)
        self.corrupt = False
        self.click(key='reload_original_download')
        self.assertTrue(self.app.get('download_button'))
        self.assertFalse(self.posts())

    def test_missing_or_mismatched_original_is_not_offered_as_result_source(self):
        self.missing_source = SOURCE
        self.page('refine'); self.click(key='detail_' + REFINE)
        self.widget('selectbox', key='result_format').set_value('원본 파일').run(); self.clean()
        self.assertFalse(self.app.get('download_button'))
        self.missing_source = ''
        value = json.loads(self.raw)
        value['source']['document_id'] = SOURCE2
        self.raw = data_api.encode(value)
        self.app = self.new_app({'page': 'refine', 'id': REFINE})
        self.widget('selectbox', key='result_format').set_value('원본 파일').run(); self.clean()
        self.assertFalse(self.app.get('download_button'))
        self.assertFalse([x for x in self.calls if x[:2] == ('DOWNLOAD', 'source-documents')])
        self.assertFalse(self.posts())

    def test_partial_result_and_timing_panel_are_read_only_and_collapsed(self):
        value = json.loads(self.raw)
        value['timings'] = {'version': 'refinement-timing-v1', 'native_parse_ms': 1500,
                            'ocr_ms': 2000, 'parser_total_ms': 3500}
        value['ocr'] = {'pages': [{'page': 1, 'status': 'partial',
                                 'timings': {'render_ms': 20, 'request_ms': 1900, 'total_ms': 1950}}]}
        value['quality'] = {'state': 'needs_ocr', 'issues': [{'code': 'ocr_visual_content_unread',
                            'requires_ocr': True, 'location': {'page': 1}}]}
        self.result_state = 'needs_ocr'
        self.raw = data_api.encode(value)
        self.page('refine'); self.click(key='detail_' + REFINE)
        panel = next(x for x in self.app.expander if x.label == '처리 시간')
        self.assertFalse(panel.proto.expanded)
        self.assertTrue(any('부분 인식 1개 페이지' in x.value for x in self.app.info))
        self.assertFalse(self.posts())
        self.assertEqual(self.app.session_state.data_console['artifact']['raw'], self.raw)

    def test_unified_intake_switching_modes_does_not_submit(self):
        self.click('자료 가져오기')
        self.assertEqual(self.widget('radio', key='request_method').value, 'crawl')
        self.widget('radio', key='request_method').set_value('upload').run(); self.clean()
        self.assertEqual(len(self.app.get('file_uploader')), 1)
        self.assertFalse([x for x in self.app.text_input if x.key == 'crawl_url'])
        self.widget('radio', key='request_method').set_value('crawl').run(); self.clean()
        self.assertEqual(self.widget('text_input', key='crawl_url').value, '')
        self.assertFalse(self.posts())

    def test_incomplete_recognition_is_not_presented_as_fully_read(self):
        self.result_state = 'needs_ocr'
        raw = json.loads(self.raw)
        raw['quality'] = {'state': 'needs_ocr', 'issues': [{'code': 'ocr_timeout', 'requires_ocr': True, 'location': {'page': 1}}]}
        self.raw = data_api.encode(raw)
        self.page('refine'); self.click(key='detail_' + REFINE)
        self.assertIn('일부 내용 미확인', [m.value for m in self.app.metric])
        self.assertTrue(any('읽지 못했습니다' in w.value for w in self.app.warning))
        self.assertTrue(any('페이지 1' in w.value for w in self.app.text))
        self.assertFalse(any('ocr_timeout' in w.value for w in self.app.text))

    def test_result_shows_actual_recognition_page_completion(self):
        raw = json.loads(self.raw)
        raw['ocr'] = {'pages': [{'page': 1, 'status': 'completed'}, {'page': 2, 'status': 'failed'}]}
        self.raw = data_api.encode(raw)
        self.page('refine'); self.click(key='detail_' + REFINE)
        self.assertTrue(any('글자 인식 결과: 완료 1개 페이지 · 읽기 실패 1개 페이지' == x.value for x in self.app.info))
        self.assertFalse(self.posts())

    def test_corrupt_result_is_not_previewed_or_downloaded(self):
        self.corrupt = True
        self.page('refine'); self.click(key='detail_' + REFINE)
        self.assertIsNone(self.app.session_state.data_console['artifact'])
        self.assertFalse(self.app.get('download_button'))
        self.assertTrue(self.app.error)

    def test_old_operation_link_is_readable_without_exposing_a_fourth_menu(self):
        self.app = self.new_app({'page': 'operation', 'id': OP})
        self.assertEqual(len(self.widget('radio', key='data_nav').options), 2)
        self.click('자료와 결과 보기')
        self.assertEqual(self.app.session_state.data_console['selected'], REFINE)
        self.assertFalse(self.posts())

    def test_upload_stops_at_source_and_explicit_refinement_reuses_deployment(self):
        import streamlit as st
        class Upload:
            name = '원본.txt'
            def getvalue(self): return b'original'
        self.page('source')
        with patch.object(st, 'file_uploader', return_value=Upload()):
            self.upload_form()
            self.click('가져오기')
        self.assertEqual([x[1] for x in self.posts()], ['upload'])
        self.click('내용 정리하기')
        self.assertEqual([x[1] for x in self.posts()], ['upload', 'refine'])
        self.assertEqual(self.posts()[1][2]['document_id'], SOURCE)
        self.assertNotIn('ocr_deployment_id', self.posts()[1][2])

    def test_source_shows_page_content_without_http_response_controls(self):
        self.page('source'); self.click(key='detail_' + SOURCE)
        self.assertEqual(self.app.session_state.data_console['artifact']['raw'], b'dom')
        self.assertFalse([x for x in self.app.radio if str(x.key).startswith('representation_')])
        self.assertFalse(self.posts())

    def test_corrupt_download_is_not_offered(self):
        self.corrupt = True
        self.page('source'); self.click(key='detail_' + SOURCE)
        self.assertIsNone(self.app.session_state.data_console['artifact'])
        self.assertEqual(len(self.app.get('download_button')), 0)

    def test_success_result_opens_readable_body_and_table_with_quality_warning(self):
        self.page('refine'); self.click(key='detail_' + REFINE)
        self.assertIn('완료', [m.value for m in self.app.metric])
        self.assertIn('내용 확인 필요', [m.value for m in self.app.metric])
        markup = '\n'.join(str(x.proto) for x in self.app.get('html'))
        self.assertIn('&lt;script&gt;', markup)
        self.assertIn('한글', markup)
        self.assertFalse(any(EVIL in str(x.proto) for x in self.app.get('html')))
        self.assertEqual(len(self.app.json), 0)
        self.assertEqual(len(self.app.code), 0)
        self.assertEqual(self.app.session_state.data_console['artifact']['raw'], self.raw)
        self.assertFalse(self.posts())

    def test_running_phase_refresh_then_terminal_stops_polling(self):
        self.running = True
        self.page('refine'); self.click(key='detail_' + REFINE)
        self.assertIn('처리 중', [m.value for m in self.app.metric])
        self.assertIn('글자와 표를 읽고 있어요', [m.value for m in self.app.info])
        self.assertFalse(self.app.session_state.data_console.get('settled'))
        self.running = False
        self.click('상태 새로고침')
        self.assertTrue(self.app.session_state.data_console['settled'])
        self.assertFalse(self.posts())

    def test_definitive_bad_request_allows_input_correction(self):
        self.reject = True
        self.crawl_form(); self.click('가져오기')
        self.click(key='close_request')
        self.click(key='new_retry_pending')
        self.assertIsNone(self.app.session_state.data_console['pending'])
        self.assertNotIn('pending', self.app.query_params)
        self.assertEqual(self.widget('text_input', key='crawl_url').value, 'https://example.com')
        self.reject = False
        self.widget('text_input', key='crawl_url').set_value('https://corrected.example')
        self.click('가져오기')
        self.assertNotEqual(self.posts()[0][3], self.posts()[1][3])

    def test_rejected_pdf_upload_explains_stage_and_preserves_server_reason(self):
        import streamlit as st
        class Upload:
            name = 'AI_Engineer_sample2.pdf'
            def getvalue(self): return b'private-pdf-bytes'
        self.reject = True
        self.rejection_message = 'file structure does not match its extension'
        with patch.object(st, 'file_uploader', return_value=Upload()):
            self.upload_form(); self.click('가져오기')
        self.assertTrue(any('내용 정리는 아직 시작하지 않았습니다' in e.value for e in self.app.error))
        self.assertTrue(any('파일 형식 검사' in e.value for e in self.app.error))
        details = '\n'.join(x.value for x in self.app.code)
        self.assertIn('HTTP 400', details)
        self.assertIn('InvalidArgument: ' + self.rejection_message, details)
        self.assertNotIn('private-pdf-bytes', details)
        self.assertIn('문제 해결 정보', [x.label for x in self.app.expander])
        self.app.run(); self.clean()
        self.assertEqual([x[1] for x in self.posts()], ['upload'])
        self.assertEqual(self.app.session_state.data_console['payload'][2], b'private-pdf-bytes')
        self.click('입력 수정')
        self.assertFalse(self.app.code)
        self.assertIsNone(self.app.session_state.data_console['pending'])

    def test_unknown_server_reason_is_plain_text_and_never_active_html(self):
        self.reject = True
        self.rejection_message = EVIL
        self.crawl_form(); self.click('가져오기')
        self.assertTrue(any(EVIL in x.value for x in self.app.code))
        self.assertFalse(any(EVIL in str(x.proto) for x in self.app.get('html')))
        self.assertEqual(len(self.posts()), 1)


if __name__ == '__main__':
    unittest.main()
