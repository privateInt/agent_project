"""Check that authored quality probes reject realistic false positives."""
import copy
import json
import unittest

import audit_document_quality as audit


class QualityAuditTests(unittest.TestCase):
    def setUp(self):
        self.ref = json.loads(audit.REFERENCE.read_text())
        def block(kind, region, **fields):
            return {'type': kind, 'location': {'page': region['page'], 'bbox': region['bbox']}, **fields}
        cells = []
        row = 0
        for key, cols in [('processes', 'process_columns'), ('triplets', 'value_columns')]:
            for values in self.ref['table1'][key]:
                for col, text in zip(self.ref['table1'][cols], values):
                    cells.append({'row': row, 'column': col, 'text': text})
                row += 1
        self.value = {'blocks': [
            block('paragraph', self.ref['figure'], text='\n'.join(self.ref['figure']['labels'])),
            block('table', self.ref['table1'], cells=cells),
            block('table', self.ref['table2'], cells=copy.deepcopy(self.ref['table2']['cells']))],
            'quality': {'state': 'needs_review', 'issues': []}}
        for i, b in enumerate(self.value['blocks']):
            b.update(id=f'b{i+1}', order=i)

    def check(self, name):
        return next(c for c in audit.checks_for(self.value, self.ref) if c['check'] == name)

    def test_authored_answers_pass(self):
        self.assertTrue(all(x['passed'] for x in audit.checks_for(self.value, self.ref)))

    def test_raw_html_or_caption_cannot_satisfy_missing_diagram(self):
        self.value['ocr'] = {'raw_html': self.value['blocks'][0]['text']}
        self.value['blocks'][0]['text'] = 'Figure 1. The system boundaries'
        result = self.check('diagram_internal_labels')
        self.assertFalse(result['passed'])
        self.assertEqual(result['matched'], 0)

    def test_washing_ii_does_not_match_washing_i(self):
        self.value['blocks'][0]['text'] = '\n'.join(x for x in self.ref['figure']['labels'] if x != 'Washing I')
        self.assertEqual(self.check('diagram_internal_labels')['missing'], ['Washing I'])

    def test_correct_words_elsewhere_do_not_count_as_diagram(self):
        self.value['blocks'][0]['location'] = {'page': 3, 'bbox': [20, 20, 500, 40]}
        self.assertEqual(self.check('diagram_internal_labels')['matched'], 0)

    def test_same_values_in_wrong_columns_fail(self):
        cells = self.value['blocks'][1]['cells']
        target = next(c for c in cells if c['text'] == 'Chromium VI')
        target['column'] = 4
        result = self.check('table1_item_amount_unit')
        self.assertFalse(result['passed'])
        self.assertIn(['Chromium VI', '47.44', 'Kg'], result['missing'])

    def test_process_label_is_not_satisfied_by_material_name(self):
        target = next(c for c in self.value['blocks'][1]['cells'] if c['text'] == 'Sammying')
        target['text'] = 'Cowhide, sammying'
        self.assertFalse(self.check('table1_process_labels')['passed'])

    def test_changed_exponent_missing_cell_or_bad_span_fails_full_table(self):
        original = copy.deepcopy(self.value['blocks'][2]['cells'])
        for change in ('number', 'missing', 'span', 'extra'):
            with self.subTest(change=change):
                cells = copy.deepcopy(original)
                if change == 'number':
                    next(c for c in cells if c['text'] == '1.03E-17')['text'] = '1.03E+17'
                elif change == 'missing':
                    cells.pop()
                elif change == 'span':
                    next(c for c in cells if c['text'] == 'Climate change')['row_span'] = 19
                else:
                    cells.append({'row': 26, 'column': 0, 'text': 'extra'})
                self.value['blocks'][2]['cells'] = cells
                self.assertFalse(self.check('table2_all_cells_and_spans')['passed'])

    def test_only_nfc_spacing_normalized(self):
        self.value['blocks'][2]['cells'][0]['text'] = 'Impact\n  category'
        self.assertTrue(self.check('table2_all_cells_and_spans')['passed'])
        self.value['blocks'][2]['cells'][0]['text'] = 'IMPACT category'
        self.assertFalse(self.check('table2_all_cells_and_spans')['passed'])

    def test_overlapping_duplicate_detected_but_distant_quote_preserved(self):
        pieces = ['This sentence contains a significant amount of duplicated content.',
                  'A second sentence confirms a substantial matching body region.']
        table = {'id': 'dup', 'type': 'table', 'location': {'page': 6, 'bbox': [20, 650, 480, 690]},
                 'cells': [{'text': t} for t in pieces]}
        paragraph = {'id': 'body', 'type': 'paragraph', 'text': ' '.join(pieces),
                     'location': {'page': 6, 'bbox': [10, 630, 500, 720]}}
        self.value['blocks'].extend([paragraph, table])
        self.assertFalse(self.check('no_overlapping_table_body_duplicate')['passed'])
        table['location']['page'] = 8
        self.assertTrue(self.check('no_overlapping_table_body_duplicate')['passed'])

    def test_unread_other_page_is_not_a_quality_pass(self):
        self.value['quality']['state'] = 'needs_ocr'
        self.assertFalse(self.check('no_unresolved_ocr')['passed'])

    def test_visual_page_duplicate_is_detected_but_distant_or_different_words_are_not(self):
        a = {'id': 'page', 'type': 'paragraph', 'text': 'Register now!',
             'location': {'page': 1, 'bbox': [10, 10, 90, 30]}}
        b = dict(copy.deepcopy(a), id='crop', visual_region_id='v1')
        self.value['blocks'].extend([a, b])
        self.assertFalse(self.check('no_overlapping_visual_text_duplicate')['passed'])
        b['location']['bbox'] = [10, 50, 90, 70]
        self.assertTrue(self.check('no_overlapping_visual_text_duplicate')['passed'])
        b['location'] = copy.deepcopy(a['location']); b['text'] = 'Register later!'
        self.assertTrue(self.check('no_overlapping_visual_text_duplicate')['passed'])

    def test_wrong_source_rejected_before_scoring(self):
        with self.assertRaisesRegex(ValueError, 'hashes do not match'):
            audit.evaluate(b'another PDF', {}, '', self.ref)

    def test_overlap_or_unbounded_grid_rejected(self):
        for cells in ([{'row': 0, 'column': 0, 'text': 'a'}] * 2,
                      [{'row': 0, 'column': 0, 'row_span': 10000000, 'text': 'a'}]):
            with self.assertRaises(ValueError):
                audit.grid({'cells': cells})


if __name__ == '__main__':
    unittest.main()
