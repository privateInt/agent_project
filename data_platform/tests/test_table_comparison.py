"""Authored conflicts + real PDF extraction; inference is controlled, not GPU evidence."""
import copy
import importlib.util
import json
from pathlib import Path
import runpy
import hashlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_refinement_parser import parser, extract, pdf_fixture, borderless_fixture
from test_refinement_ocr import ocr, identity, document, response

module = importlib.util.spec_from_file_location('table_compare', Path(parser.__file__).with_name('table_compare.py'))
compare = importlib.util.module_from_spec(module)
module.loader.exec_module(compare)


def table(values=('항목', '금액', '매출', '1,200'), bbox=(10, 10, 90, 90), page=1):
    return {'type': 'table', 'location': {'page': page, 'bbox': list(bbox), 'coordinate_system': 'pdf-points-top-left'},
            'cells': [{'row': i//2, 'column': i%2, 'row_span': 1, 'column_span': 1, 'text': v}
                      for i, v in enumerate(values)]}


def reconcile(native, recognized=None, status='completed'):
    doc = parser.Document({'document_id': 'source', 'sha256': 'a'*64, 'format': 'pdf', 'image_id': 'image'})
    doc.value['pages'] = [{'number': 1, 'width': 200, 'height': 300, 'rotation': 0, 'coordinate_system': 'pdf-points-top-left'}]
    doc.value['blocks'] = copy.deepcopy(native if recognized is None else recognized)
    for i, b in enumerate(doc.value['blocks']): b.update(id='b'+str(i+1), order=i)
    if recognized is not None:
        doc.value['ocr'] = {'pages': [{'page': 1, 'status': status, 'native_blocks': copy.deepcopy(native)}]}
    compare.reconcile(doc)
    return doc.value


class ComparisonTests(unittest.TestCase):
    def prose_candidate(self):
        texts = ['It is widely projected that as the planet warms, climate and weather',
                 'variability will increase. Changes in the frequency and severity of extreme climate events and weather',
                 'patterns variability will have significant consequences for human and natural systems [18]']
        native = {'type': 'table', 'location': {'page': 1, 'bbox': [20, 50, 180, 100],
                                               'coordinate_system': 'pdf-points-top-left'},
                  'cells': [{'row': i, 'column': 1 if i == 0 else 0, 'row_span': 1,
                             'column_span': 2 if i != 1 else 3, 'text': text} for i, text in enumerate(texts)]}
        body = {'type': 'paragraph', 'text': 'Introduction. ' + ' '.join(texts) + '. More body.',
                'location': {'page': 1, 'bbox': [10, 40, 190, 120], 'coordinate_system': 'pdf-points-top-left'}}
        return native, body

    def test_sparse_native_prose_already_inside_ocr_paragraph_is_not_inserted_again(self):
        native, body = self.prose_candidate()
        result = reconcile([native], [body])
        self.assertEqual([b['type'] for b in result['blocks']], ['paragraph'])
        self.assertEqual(result['blocks'][0]['text'], body['text'])
        self.assertEqual(result['ocr']['pages'][0]['native_blocks'], [native])
        self.assertNotIn('table_comparison', result)
        self.assertIn('pdf_native_table_text_already_in_ocr_body', [i['code'] for i in result['quality']['issues']])

    def test_prose_filter_preserves_conflicts_order_noncontained_and_real_tables(self):
        for variant in ('number', 'missing', 'reordered', 'outside', 'header', 'valid_table', 'ocr_table'):
            with self.subTest(variant=variant):
                native, body = self.prose_candidate()
                recognized = [body]
                if variant == 'number': body['text'] = body['text'].replace('[18]', '[19]')
                if variant == 'missing': body['text'] = body['text'].replace(native['cells'][1]['text'], '')
                if variant == 'reordered': body['text'] = ' '.join(c['text'] for c in reversed(native['cells']))
                if variant == 'outside': body['location']['bbox'] = [30, 40, 190, 120]
                if variant == 'header': native['cells'][0]['header'] = True
                if variant == 'valid_table':
                    for cell in native['cells']: cell.update(column=0, column_span=1)
                if variant == 'ocr_table': recognized.append(copy.deepcopy(native))
                result = reconcile([native], recognized)
                self.assertTrue(any(b['type'] == 'table' for b in result['blocks']))
                self.assertNotIn('pdf_native_table_text_already_in_ocr_body', [i['code'] for i in result['quality']['issues']])

    def test_prose_filter_does_not_run_in_parser_only_or_failed_ocr(self):
        native, body = self.prose_candidate()
        self.assertEqual(reconcile([native])['blocks'][0]['type'], 'table')
        native.update(id='b1', order=0)
        result = reconcile([native], [native], status='failed')
        self.assertEqual(result['blocks'][0]['type'], 'table')

    def test_only_spacing_nfc_normalized_not_signs_case_or_punctuation(self):
        a, b = table(), table(('항 목', '금액', '매\n출', '1,200'))
        self.assertEqual(compare.decide([a], [b], True, '')['status'], 'agreed')
        for changed in ('1200', '-1,200', '1.200', '001200'):
            b['cells'][3]['text'] = changed
            result = compare.decide([a], [b], True, '')
            self.assertEqual(result['status'], 'needs_review')
            self.assertIn('numeric_conflict', result['reasons'])
        self.assertEqual(compare.normalized('가 나'), '가나')
        self.assertNotEqual(compare.normalized('A'), compare.normalized('a'))

    def test_swapped_values_are_not_hidden_by_same_numeric_bag(self):
        a, b = table(('name', 'value', '10', '20')), table(('name', 'value', '20', '10'))
        result = compare.decide([a], [b], True, '')
        self.assertEqual(result['difference_count'], 2)
        self.assertIn('numeric_conflict', result['reasons'])
        self.assertEqual(result['selected'], 'ocr')

    def test_span_conflict_never_hidden_by_identical_words(self):
        a, b = table(), table()
        a['cells'][0]['column_span'] = 2
        a['cells'].pop(1)
        result = compare.decide([a], [b], True, '')
        self.assertEqual(result['status'], 'needs_review')
        self.assertIn('structure_conflict', result['reasons'])

    def test_valid_ocr_replaces_empty_parser_and_preserves_both(self):
        a, b = table(('', '', '', '')), table()
        original = copy.deepcopy((a, b))
        result = reconcile([a], [b])
        record = result['table_comparison']['tables'][0]
        self.assertEqual((record['status'], record['selected']), ('selected', 'ocr'))
        self.assertEqual(record['parser_candidates'][0]['cells'], a['cells'])
        self.assertEqual(record['ocr_candidates'][0]['cells'], b['cells'])
        self.assertEqual((a, b), original)
        self.assertEqual(result['blocks'][0]['cells'], b['cells'])

    def test_valid_parser_replaces_empty_ocr(self):
        result = compare.decide([table()], [table(('', '', '', ''))], True, '')
        self.assertEqual(result['status'], 'selected')
        self.assertEqual(result['selected'], 'parser')
        result = compare.decide([table(('A', 'B', 'C', 'D'))], [table(('', '', '', ''))], True, '')
        self.assertEqual(result['status'], 'selected')

    def test_invalid_grid_and_numbers_cannot_be_automatically_resolved(self):
        a, b = table(), table(('항목', '금액', '매출', '9,900'))
        a['cells'][0]['column_span'] = 2
        result = compare.decide([a], [b], True, '')
        self.assertEqual((result['status'], result['selected']), ('needs_review', 'ocr'))
        self.assertIn('numeric_conflict', result['reasons'])

    def test_overlap_holes_invalid_spans_are_detected_and_bounded(self):
        for change, problem in [({'column_span': 2}, 'overlapping_cells'),
                                ({'column_span': 10**9}, 'invalid_span'),
                                ({'row': True}, 'invalid_span')]:
            a = table(); a['cells'][0].update(change)
            self.assertIn(problem, compare.assess(a)[0]['issues'])
        a = table(); a['cells'].pop(1)
        self.assertIn('missing_cells', compare.assess(a)[0]['issues'])

    def test_split_ocr_tables_stay_one_ambiguous_group(self):
        result = reconcile([table()], [table(bbox=(10,10,90,50)), table(bbox=(10,50,90,90))])
        rows = result['table_comparison']['tables']
        self.assertEqual(len(rows), 1)
        self.assertIn('ambiguous_regions', rows[0]['reasons'])
        self.assertEqual(rows[0]['selected'], 'ocr')
        self.assertEqual(len(rows[0]['ocr_candidates']), 2)
        self.assertEqual(len(result['blocks']), 2)

    def test_ocr_priority_preserves_interleaved_body_when_tables_split(self):
        first,second=table(bbox=(10,10,90,40)),table(bbox=(10,60,90,90))
        body={'type':'paragraph','text':'between tables','location':{'page':1,'bbox':[10,45,90,55]}}
        result=reconcile([table()],[first,body,second])
        self.assertEqual([b['type'] for b in result['blocks']],['table','paragraph','table'])
        self.assertEqual(result['blocks'][1]['text'],'between tables')
        row=result['table_comparison']['tables'][0]
        self.assertEqual(row['result_block_ids'],['b1','b3'])
        self.assertEqual(row['selected'],'ocr')

    def test_readable_ocr_kept_even_when_grid_is_incomplete(self):
        a,b=table(),table()
        b['cells'].pop(1)
        result=reconcile([a],[b])
        row=result['table_comparison']['tables'][0]
        self.assertEqual((row['selected'],row['status']),('ocr','needs_review'))
        self.assertEqual(result['blocks'][0]['cells'],b['cells'])
        self.assertIn('ocr_priority',row['reasons'])

    def test_multiple_tables_match_spatially_not_by_ocr_order(self):
        left, right = table(bbox=(0,0,80,80)), table(bbox=(100,0,180,80))
        result = reconcile([left, right], [right, left])
        rows = result['table_comparison']['tables']
        self.assertTrue(all(x['status'] == 'agreed' for x in rows))
        self.assertEqual([x['location']['bbox'] for x in result['blocks']], [right['location']['bbox'], left['location']['bbox']])
        self.assertEqual([r['result_block_ids'] for r in rows], [['b2'], ['b1']])

    def test_low_overlap_same_words_cannot_claim_agreement(self):
        result = reconcile([table(bbox=(0,0,80,80))], [table(bbox=(50,0,130,80))])
        self.assertIn('ambiguous_regions', result['table_comparison']['tables'][0]['reasons'])

    def test_missing_ocr_table_preserved_and_duplicate_paragraph_removed(self):
        inner = {'type': 'paragraph', 'text': 'duplicated table', 'location': {'page':1, 'bbox':[20,20,80,80]}}
        outside = {'type': 'paragraph', 'text': 'outside body', 'location': {'page':1, 'bbox':[10,100,90,120]}}
        result = reconcile([table()], [inner, outside])
        self.assertEqual([b['type'] for b in result['blocks']], ['table', 'paragraph'])
        self.assertEqual(result['blocks'][1]['text'], 'outside body')
        self.assertEqual(result['table_comparison']['tables'][0]['reasons'], ['ocr_missing'])
        self.assertEqual(result['quality']['state'], 'needs_review')

    def test_ocr_only_and_no_ocr_never_claim_comparison_passed(self):
        for native, recognized, reason in [([], [table()], 'parser_missing'), ([table()], None, 'ocr_not_requested')]:
            result = reconcile(native, recognized)
            row = result['table_comparison']['tables'][0]
            self.assertEqual(row['status'], 'needs_review')
            self.assertIn(reason, row['reasons'])

    def test_differences_clipped_but_complete_candidates_preserved(self):
        a = table(tuple('A' for _ in range(202)))
        b = table(tuple('B' for _ in range(202)))
        result = reconcile([a], [b])['table_comparison']['tables'][0]
        self.assertEqual(result['difference_count'], 202)
        self.assertEqual(len(result['differences']), 100)
        self.assertTrue(result['differences_truncated'])
        self.assertEqual(len(result['ocr_candidates'][0]['cells']), 202)


class PipelineTests(unittest.TestCase):
    def extract_with_answer(self, raw, answer):
        original_run = runpy.run_path
        calls = Mock(return_value=ocr.decode_response(json.dumps(response(answer)).encode(), identity()['served_model_name']))
        def loader(path, *args, **kwargs):
            helper = original_run(path, *args, **kwargs)
            if Path(path).name == 'ocr.py':
                helper['enrich'] = lambda doc, data, pinned, measure_timings=False: ocr.enrich(doc, data, pinned, infer_page=calls, measure_timings=measure_timings)
            return helper
        with patch('runpy.run_path', side_effect=loader): result = extract(raw, 'pdf', ocr=identity(), measure_timings=True)
        return result, calls

    def test_real_pdf_merge_geometry_restores_row_and_column_spans(self):
        raw = pdf_fixture(content=b'0 0 0 RG 30 100 160 80 re S 30 140 m 190 140 l S 110 100 m 110 140 l S\nBT /F1 12 Tf 40 150 Td (Header) Tj ET\nBT /F1 12 Tf 40 110 Td (A) Tj ET\nBT /F1 12 Tf 120 110 Td (B) Tj ET')
        result = extract(raw, 'pdf')
        cells = next(b for b in result['blocks'] if b['type'] == 'table')['cells']
        self.assertEqual([(c['row'],c['column'],c['row_span'],c['column_span']) for c in cells], [(0,0,1,2),(1,0,1,1),(1,1,1,1)])

    def test_pipeline_one_inference_for_table_page_and_bound_result_references(self):
        raw = pdf_fixture(table=True)
        original = extract(raw, 'pdf')
        native = next(b for b in original['blocks'] if b['type'] == 'table')
        page = original['pages'][0]
        bbox = ' '.join(str(v/page['width' if i%2 == 0 else 'height']*1000) for i,v in enumerate(native['location']['bbox']))
        result, calls = self.extract_with_answer(raw, '<div data-label="Table" data-bbox="'+bbox+'"><table><tr><td>A1</td><td>B1</td></tr><tr><td>A2</td><td>B2</td></tr></table></div>')
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(result['processing']['mode'], 'ocr_priority')
        row = result['table_comparison']['tables'][0]
        self.assertEqual((row['status'], row['selected']), ('agreed','ocr'))
        self.assertEqual(row['result_block_ids'], ['b1'])
        self.assertEqual(result['blocks'][0]['table_comparison_id'], row['id'])
        self.assertGreaterEqual(result['timings']['table_comparison_ms'], 0)

    def test_borderless_native_candidate_is_derived_without_extra_inference(self):
        raw = borderless_fixture()
        answer = '<div data-label="Table" data-bbox="70 90 950 750"><table>' + ''.join(
            '<tr><td>Water</td><td>'+str(10+i)+'</td><td>kg</td></tr>' for i in range(6)) + '</table></div>'
        result, calls = self.extract_with_answer(raw, answer)
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(result['processing']['mode'], 'ocr_priority')
        row = result['table_comparison']['tables'][0]
        self.assertEqual(len(row['parser_candidates']), 1)
        self.assertEqual(row['parser_candidates'][0]['extraction'], 'pdf-native-text-grid-v1')
        self.assertIn('parser_grid_inferred', row['reasons'])
        self.assertEqual((row['status'],row['selected']), ('needs_review','ocr'))
        self.assertIn('Water', ' '.join(c['text'] for c in row['parser_candidates'][0]['cells']))
        self.assertNotEqual(row['parser_candidates'][0]['cells'], row['ocr_candidates'][0]['cells'])

    def test_inferred_grid_never_displaces_conflicting_ocr_as_more_accurate(self):
        a,b=table(),table()
        a['extraction']='pdf-native-text-grid-v1'
        b['cells'].pop(1)
        result=compare.decide([a],[b],True,'')
        self.assertEqual((result['status'],result['selected']),('needs_review','ocr'))
        self.assertIn('ocr_invalid',result['reasons'])

    def test_isolated_pdf_cli_loads_comparison_helper(self):
        raw=pdf_fixture(table=True)
        spec={'document_id':'11111111-1111-4111-8111-111111111111','sha256':hashlib.sha256(raw).hexdigest(),
              'format':'pdf','image_id':'sha256:'+'a'*64,'rules_version':parser.RULES}
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'source').write_bytes(raw)
            (root/'spec.json').write_text(json.dumps(spec))
            subprocess.run([sys.executable,'-I','-B',parser.__file__,'--input',str(root/'source'),
                            '--spec',str(root/'spec.json'),'--output',str(root/'result.json')],check=True,
                           capture_output=True,timeout=30)
            value=json.loads((root/'result.json').read_text())
        self.assertEqual(value['table_comparison']['tables'][0]['selected'],'parser')
        self.assertEqual(value['table_comparison']['tables'][0]['result_block_ids'],['b2'])

    def test_parser_only_scanned_pdf_never_loads_ocr_or_hides_missing_content(self):
        original=runpy.run_path
        def loader(path):
            self.assertNotEqual(Path(path).name,'ocr.py')
            return original(path)
        with patch('runpy.run_path',side_effect=loader):
            value=extract(pdf_fixture(text=False),'pdf')
        self.assertEqual(value['processing']['mode'],'parser_only')
        self.assertEqual(value['quality']['state'],'needs_ocr')
        self.assertNotIn('ocr',value)

    def test_native_only_no_ocr_requests_and_legacy_non_pdf_unchanged(self):
        value = extract(pdf_fixture(table=True), 'pdf')
        self.assertNotIn('ocr', value)
        self.assertEqual(value['processing']['mode'],'parser_only')
        self.assertEqual(value['table_comparison']['tables'][0]['reasons'], ['ocr_not_requested'])
        self.assertNotIn('table_comparison', extract(b'<table><tr><td>A</td></tr></table>', 'html'))


if __name__ == '__main__':
    unittest.main()
