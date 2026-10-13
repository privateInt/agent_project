"""Bounded multipart intake for the first PDF/TXT/Markdown product scope."""
import hashlib
import re
import unicodedata
from email import policy
from email.errors import UndecodableBytesDefect
from email.parser import BytesParser

from .contracts import ContractError, MAX_BYTES, upload_metadata

MAX_REQUEST = MAX_BYTES + (64 << 10)


class UploadError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def check(condition, code, status=400):
    if not condition:
        raise UploadError(code, status)


def metadata(name, filename, data):
    check(isinstance(name, str) and isinstance(filename, str), 'UPLOAD_METADATA_INVALID')
    name = name.strip()
    try:
        check(1 <= len(name) <= 200 and not any(unicodedata.category(c) == 'Cc' for c in name),
              'UPLOAD_NAME_INVALID')
        check(1 <= len(filename.encode('utf-8')) <= 255 and filename.strip() == filename
              and ':' not in filename and not any(unicodedata.category(c) == 'Cc' for c in filename),
              'UPLOAD_FILENAME_INVALID')
        check(isinstance(data, bytes) and 0 < len(data) <= MAX_BYTES, 'UPLOAD_BYTES_INVALID', 413)
        result = upload_metadata(name, filename, len(data), hashlib.sha256(data).hexdigest(), 0)
    except (UnicodeError, ContractError) as exc:
        raise UploadError(str(exc) if isinstance(exc, ContractError) else 'UPLOAD_METADATA_INVALID') from None
    # An intake signature is not a promise that later extraction succeeds.
    prefix = data[3:] if data.startswith(b'\xef\xbb\xbf') else data
    offset = 0
    while offset < min(len(prefix), 1024) and prefix[offset] in b'\x00\t\n\f\r ':
        offset += 1
    signature = prefix[offset:offset + 9]
    pdf = (len(signature) >= 8 and (signature[:7] == b'%PDF-1.' and signature[7:8] in b'01234567'
           or signature[:8] == b'%PDF-2.0')
           and (len(signature) == 8 or signature[8] in b'\x00\t\n\f\r '))
    if filename.rsplit('.', 1)[-1].lower() == 'pdf':
        check(pdf, 'UPLOAD_FORMAT_MISMATCH')
    else:
        try:
            text = data.decode('utf-8')
        except UnicodeError:
            raise UploadError('UPLOAD_TEXT_NOT_UTF8') from None
        check(not pdf and not data.startswith((b'%PDF-', b'PK\x03\x04'))
              and not any(unicodedata.category(c) == 'Cc' and c not in '\n\r\t\f' for c in text),
              'UPLOAD_FORMAT_MISMATCH')
    return result


def multipart(content_type, raw):
    """Parse the entire bounded body before committing any job or calling Data."""
    check(isinstance(raw, bytes) and 0 < len(raw) <= MAX_REQUEST, 'UPLOAD_REQUEST_TOO_LARGE', 413)
    check(isinstance(content_type, str) and len(content_type) <= 512
          and '\r' not in content_type and '\n' not in content_type, 'MULTIPART_INVALID')
    try:
        header = content_type.encode('ascii')
        message = BytesParser(policy=policy.default).parsebytes(
            b'Content-Type: ' + header + b'\r\nMIME-Version: 1.0\r\n\r\n' + raw)
        check(message.get_content_type() == 'multipart/form-data', 'MULTIPART_INVALID')
        boundary = message.get_boundary()
        check(isinstance(boundary, str) and re.fullmatch(r"[A-Za-z0-9'()+_,./:=? -]{1,70}", boundary)
              and not boundary.endswith(' '), 'MULTIPART_INVALID')
        check(message.is_multipart() and not message.defects
              and not (message.preamble or '').strip() and not (message.epilogue or '').strip(),
              'MULTIPART_INVALID')
        parts = list(message.iter_parts())
        check(len(parts) == 2, 'MULTIPART_INVALID')
        values = {}
        for part in parts:
            check(not part.is_multipart() and not part.defects
                  and len(part.get_all('Content-Disposition', [])) == 1
                  and len(part.get_all('Content-Type', [])) <= 1
                  and all(k.lower() in ('content-disposition', 'content-type') for k in part.keys()),
                  'MULTIPART_INVALID')
            disposition = part['Content-Disposition']
            check(all(isinstance(defect, UndecodableBytesDefect) for defect in disposition.defects)
                  and part.get_content_disposition() == 'form-data', 'MULTIPART_INVALID')
            # Check the original syntax too: the email parser otherwise collapses duplicate parameters.
            original = next(value for key, value in part.raw_items() if key.lower() == 'content-disposition')
            # Browsers use UTF-8 in quoted filenames; reject ill-formed byte sequences.
            original = original.encode('utf-8', 'surrogateescape').decode('utf-8')
            syntax = re.sub(r'"(?:[^"\\]|\\.)*"', '""', original)
            check(len(re.findall(r';\s*name\s*=', syntax, re.I)) == 1
                  and len(re.findall(r';\s*filename(?:\*)?\s*=', syntax, re.I)) <= 1,
                  'MULTIPART_INVALID')
            params = dict(disposition.params)
            check(set(params) <= {'name', 'filename'} and params.get('name') in ('name', 'file'),
                  'MULTIPART_INVALID')
            field = params['name']
            check(field not in values and (field == 'file') == ('filename' in params), 'MULTIPART_INVALID')
            data = part.get_payload(decode=True)
            check(isinstance(data, bytes), 'MULTIPART_INVALID')
            if field == 'name':
                check(len(data) <= 800, 'UPLOAD_NAME_INVALID')
                values[field] = data.decode('utf-8')
            else:
                values[field] = (params['filename'], data)
        check(set(values) == {'name', 'file'}, 'MULTIPART_INVALID')
        filename, data = values['file']
        return metadata(values['name'], filename, data), data
    except (ValueError, UnicodeError, AttributeError, TypeError, RecursionError):
        raise UploadError('MULTIPART_INVALID') from None


def encode_multipart(meta, data, key):
    """Deterministic envelope and untouched bytes for a stored Data upload intent."""
    boundary = 'document-qa-' + key
    # Do not let a file containing this exact delimiter change multipart semantics.
    while ('\r\n--' + boundary).encode() in data or data.startswith(('--' + boundary).encode()):
        boundary += '-x'
        check(len(boundary) <= 70, 'MULTIPART_BOUNDARY_COLLISION')
    filename = meta['filename'].replace('\\', '\\\\').replace('"', '\\"')
    raw = (f'--{boundary}\r\nContent-Disposition: form-data; name="name"\r\n\r\n'.encode()
           + meta['name'].encode('utf-8')
           + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
             'Content-Type: application/octet-stream\r\n\r\n'.encode('utf-8')
           + data + f'\r\n--{boundary}--\r\n'.encode())
    return 'multipart/form-data; boundary=' + boundary, raw
