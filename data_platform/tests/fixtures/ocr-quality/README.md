# Korean OCR quality fixtures

Four synthetic PDF cases, five A4 pages. Four pages are image-only scans at
180 DPI; mixed-pages.pdf page 1 retains its native searchable Korean text.
The optional combined preview.pdf is no longer distributed; inspect the four case PDFs directly.

suite.json contains authored text, reading-order markers, reference regions,
table positions and merge spans, expected OCR page selection, file hashes and
generation package versions. It is independent of model inference. Whitespace
is ignored by the NFC CER rubric; table cells are scored separately from prose.

Run the prebuilt files using tests/live_ocr_quality.py. No font or PDF package
installation is needed on the server. See docs/data-platform/testing-ocr-quality.md
from the project root for execution and result interpretation.

For fixture maintenance only, tests/generate_ocr_quality_fixtures.py uses
ReportLab 4.4.9, pypdf 6.10.0, pypdfium2 5.3.0 and Pillow 12.3.0 in the authoring
environment. Regeneration replaces the PDFs and suite.json; never regenerate
them during an existing quality run. Verify the new pages and version the
suite/rubric intentionally if the reference or criteria change.

The authoring-only Nanum Gothic Regular TTF is no longer distributed in this project.
Prebuilt fixture execution does not need it. The font license is retained in
fonts/OFL.txt because the authored PDFs contain embedded font subsets.
The font was obtained from the [Google Fonts source](https://github.com/google/fonts/tree/main/ofl/nanumgothic).
To regenerate fixtures, first restore fonts/NanumGothic-Regular.ttf with SHA-256
76f45ef4a6bcff344c837c95a7dcc26e017e38b5846d5ae0cdcb5b86be2e2d31
(the value recorded in suite.json). The generator intentionally requires this
external prerequisite; it must not be run during ordinary tests. Regeneration
creates preview.pdf again; remove the optional preview and TTF after reviewing
and versioning new fixtures. See docs/testing/artifact-retention.md from the
project root. These documents contain invented test
content and do not represent production-document accuracy.
