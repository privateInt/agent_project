"""Recreate synthetic PNG and image-only PDF fixtures with Pillow 12.3.0."""
from pathlib import Path
import zlib
from PIL import Image, ImageDraw, ImageFont


def generate(directory):
    directory.mkdir(parents=True, exist_ok=True)
    image = Image.new('RGB', (1200, 800), 'white')
    draw = ImageDraw.Draw(image)
    title = ImageFont.load_default(size=64)
    body = ImageFont.load_default(size=38)
    draw.text((80, 60), 'OCR TEST 12345', fill='black', font=title)
    draw.text((80, 170), 'Synthetic document for OCR verification.', fill='black', font=body)
    draw.text((80, 240), 'The original image must remain unchanged.', fill='black', font=body)
    for x in (80, 650, 1080): draw.line((x, 360, x, 660), fill='black', width=3)
    for y in (360, 460, 560, 660): draw.line((80, y, 1080, y), fill='black', width=3)
    for xy, text in [((110, 390), 'ITEM'), ((680, 390), 'COUNT'), ((110, 490), 'APPLE'),
                     ((680, 490), '12'), ((110, 590), 'ORANGE'), ((680, 590), '34')]:
        draw.text(xy, text, fill='black', font=body)
    image.save(directory / 'ocr-smoke.png', 'PNG')
    pixels = zlib.compress(image.tobytes())
    content = b'q 600 0 0 400 0 0 cm /Im0 Do Q\n'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 400] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /XObject /Subtype /Image /Width 1200 /Height 800 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length ' + str(len(pixels)).encode() + b' >>\nstream\n' + pixels + b'\nendstream',
               b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n' + content + b'endstream']
    data, offsets = b'%PDF-1.4\n', [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(index).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
    xref = len(data)
    data += b'xref\n0 6\n0000000000 65535 f \n'
    data += b''.join(('%010d 00000 n \n' % offset).encode() for offset in offsets[1:])
    data += b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n' + str(xref).encode() + b'\n%%EOF\n'
    (directory / 'ocr-smoke.pdf').write_bytes(data)
    image.close()


if __name__ == '__main__':
    generate(Path(__file__).resolve().parent / 'fixtures/ocr')
