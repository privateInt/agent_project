# Rotation, viewport and JPEG fixtures

Seven inputs: PDF Rotate 90/180/270; cropped PDF; equal-size shifted MediaBox;
ordinary JPEG and EXIF orientation 6 JPEG. The suite uses ocr-geometry-v1.
Five cases measure OCR quality; two explicitly expect preserved coordinate
rejection (GUARD_PASS, not OCR success). See docs/data-platform/testing-ocr-geometry.md
from the project root.

Each PDF rotation is countered in its embedded raster, yielding upright visual
content. This isolates PDF page transformation from sideways-text recognition.
The EXIF 6 JPEG stores the same upright pixels as the ordinary JPEG, deliberately
testing the raw-pixel contract without automatic orientation adjustment.
Cropping and shifting keep all reference text visible in a viewer.

reference.png is an independently rendered upright reference used only by
local pixel-mapping tests; it is never uploaded as an OCR case. The optional
combined preview.pdf is no longer distributed. Inspect the seven inputs
directly. All text is authored synthetic content, not model output.

Generation: tests/generate_ocr_geometry_fixtures.py, using the versions in
suite.json. To regenerate, first restore the matching Nanum Gothic TTF under
../ocr-quality/fonts/ as described in the Korean quality fixture README.
The authoring-only TTF and combined previews are omitted from distribution;
the OFL license remains. Regeneration is unnecessary on the server and must not
be performed while a recorded run is pending. File hashes bind each input to
the report; thresholds and guard expectations are fixed before inference.
