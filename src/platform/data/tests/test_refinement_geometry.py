"""Real image/PDF decoding and mapping with mocked inference, not GPU evidence."""
import copy
import html
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from PIL import Image, ImageChops, ImageStat
from test_refinement_ocr import ocr, document, identity, response
from ocr_quality import evaluate_case, load_suite

ROOT = Path(__file__).resolve().parent / 'fixtures/ocr-geometry'


class RefinementGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite, _ = load_suite(ROOT / 'suite.json')

    def test_pdf_rotations_render_at_reference_pixel_positions(self):
        for case in self.suite['cases'][:3]:
            with self.subTest(case=case['id']):
                doc, data = document((ROOT / case['file']).read_bytes(), 'pdf')
                page = doc.value['pages'][0]
                self.assertEqual(page['rotation'], case['pages'][0]['rotation'])
                with ocr.render_page(data, 'pdf', page) as actual, Image.open(ROOT / 'reference.png') as reference:
                    expected = reference.resize(actual.size)
                    difference = ImageStat.Stat(ImageChops.difference(actual, expected)).mean
                    self.assertLess(max(difference), 0.5, difference)

    def test_jpeg_exif_does_not_change_stored_pixel_coordinates(self):
        rendered = []
        for case in self.suite['cases'][-2:]:
            doc, data = document((ROOT / case['file']).read_bytes(), 'jpeg')
            with Image.open(ROOT / case['file']) as source:
                self.assertEqual(source.getexif()[274], case['source_exif_orientation'])
                with ocr.render_page(data, 'jpeg', doc.value['pages'][0]) as actual:
                    self.assertEqual(actual.size, source.size)
                    self.assertEqual(actual.tobytes(), source.convert('RGB').tobytes())
                    rendered.append(actual.tobytes())
        self.assertEqual(*rendered)

    def test_crop_and_equal_size_shift_retain_native_without_inference(self):
        for case in self.suite['cases'][3:5]:
            with self.subTest(case=case['id']):
                doc, data = document((ROOT / case['file']).read_bytes(), 'pdf')
                before = copy.deepcopy(doc.value)
                infer = Mock(side_effect=AssertionError('coordinate guard must precede inference'))
                ocr.enrich(doc, data, identity(), infer_page=infer)
                infer.assert_not_called()
                self.assertEqual(doc.value['blocks'], before['blocks'])
                self.assertEqual(doc.value['quality']['state'], 'needs_ocr')
                result = evaluate_case(case, doc.value, identity())
                self.assertTrue(result['passed'], result)
                self.assertFalse(result['ocr_succeeded'])

    def test_rotation_mismatch_rejected_even_when_dimensions_match(self):
        case = self.suite['cases'][1]
        doc, data = document((ROOT / case['file']).read_bytes(), 'pdf')
        wrong = dict(doc.value['pages'][0], rotation=0)
        with self.assertRaises(ocr.OCRFailure) as caught:
            ocr.render_page(data, 'pdf', wrong)
        self.assertEqual(caught.exception.reason, 'pdf_coordinate_mismatch')

    def test_actual_renderer_and_html_adapter_match_authored_regions(self):
        for case in [*self.suite['cases'][:3], *self.suite['cases'][-2:]]:
            with self.subTest(case=case['id']):
                kind = 'pdf' if case['file'].endswith('.pdf') else 'jpeg'
                doc, data = document((ROOT / case['file']).read_bytes(), kind)
                page = case['pages'][0]
                parts = []
                for anchor in page['anchors']:
                    bbox = ' '.join(str(round(v / (page['width'] if i % 2 == 0 else page['height']) * 1000, 4))
                                    for i, v in enumerate(anchor['bbox']))
                    parts.append('<div data-label="Text" data-bbox="' + bbox + '"><p>' + html.escape(anchor['text']) + '</p></div>')
                answer = ocr.decode_response(json.dumps(response(''.join(parts))).encode(), identity()['served_model_name'])
                infer = Mock(return_value=answer)
                ocr.enrich(doc, data, identity(), infer_page=infer)
                self.assertEqual(infer.call_count, 1)
                result = evaluate_case(case, doc.value, identity())
                self.assertTrue(result['passed'], result)


if __name__ == '__main__':
    unittest.main()
