"""Local fixture authoring checks; requires the CPU parser's dependencies."""
import io
import unittest
import pdfplumber
from live_ocr_quality import DEFAULT_SUITE
from ocr_quality import load_suite, normalize
from test_refinement_parser import extract


class QualityFixtureTests(unittest.TestCase):
    def test_real_pdf_text_layers_and_cpu_page_selection(self):
        suite, _ = load_suite(DEFAULT_SUITE)
        total_pages = 0
        for case in suite['cases']:
            with self.subTest(case=case['id']):
                data = (DEFAULT_SUITE.parent / case['file']).read_bytes()
                with pdfplumber.open(io.BytesIO(data)) as pdf:
                    self.assertEqual(len(pdf.pages), len(case['pages']))
                    for page, expected in zip(pdf.pages, case['pages']):
                        total_pages += 1
                        text = normalize(page.extract_text() or '')
                        if expected['mode'] == 'native':
                            self.assertEqual(text, normalize(expected['text']))
                        else:
                            self.assertEqual(text, '')
                            self.assertEqual(len(page.images), 1)
                canonical = extract(data, 'pdf')
                selected = sorted({i.get('location', {}).get('page') for i in canonical['quality']['issues'] if i['requires_ocr']})
                self.assertEqual(selected, [p['number'] for p in case['pages'] if p['mode'] == 'ocr'])
        self.assertEqual(total_pages, 5)


if __name__ == '__main__':
    unittest.main()
