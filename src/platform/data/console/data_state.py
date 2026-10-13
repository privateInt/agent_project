"""Immutable request intent; persisted URL carries only IDs and digests."""
import base64
import hashlib
import json
import re
import uuid

from data_api import MAX_BYTES, encode, identifier

PAGES = {'crawl': '웹 가져오기 내역', 'source': '자료 목록', 'refine': '결과 확인', 'operation': '진행 상황'}
COLLECTION = {'crawl': 'crawl-runs', 'source': 'source-documents', 'refine': 'refinement-runs', 'operation': 'operations'}
ID_FIELD = {'crawl': 'crawl_run_id', 'source': 'document_id', 'refine': 'refinement_run_id', 'operation': 'operation_id'}


def navigation_page(page):
    """Keep existing detail/recovery URLs behind two user-facing menus."""
    return 'refine' if page in ('refine', 'operation') else 'source'
TERMINAL = {'succeeded', 'failed', 'cancelled'}


def validate(kind, body, file=None):
    if kind not in ('crawl', 'upload', 'refine'):
        raise ValueError('지원하지 않는 요청입니다.')
    if not isinstance(body.get('name'), str) or not body['name'].strip() or len(body['name']) > 200:
        raise ValueError('이름은 1~200자로 입력하세요.')
    if kind == 'upload':
        filename = body.get('filename', '')
        if not isinstance(file, bytes) or not 0 < len(file) <= MAX_BYTES or not filename or any(c in filename for c in '\r\n"\\/') or any(ord(c) < 32 for c in filename):
            raise ValueError('파일 이름과 크기를 확인하세요. 빈 파일은 지원하지 않으며 최대 32 MiB입니다.')
    elif kind == 'refine':
        identifier(body.get('document_id'))
        if body.get('ocr_deployment_id'):
            identifier(body['ocr_deployment_id'])
    else:
        from urllib.parse import urlsplit
        p = urlsplit(body.get('url', ''))
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
            raise ValueError('공개 HTTP(S) URL을 입력하세요.')


def fingerprint(kind, body, file=None):
    validate(kind, body, file)
    description = {'kind': kind, 'body': body}
    if kind == 'upload':
        description['file_sha256'] = hashlib.sha256(file).hexdigest()
        description['file_size'] = len(file)
    return hashlib.sha256(encode(description)).hexdigest()


def ticket(kind, body, base, file=None):
    return {'v': 1, 'kind': kind, 'key': str(uuid.uuid4()),
            'digest': fingerprint(kind, body, file), 'gateway': hashlib.sha256(base.encode()).hexdigest(),
            'target': body['document_id'] if kind == 'refine' else ''}


def pack(value):
    return base64.urlsafe_b64encode(encode(value)).decode().rstrip('=')


def unpack(value):
    try:
        if len(value) > 1200 or not re.fullmatch('[A-Za-z0-9_-]+', value):
            raise ValueError()
        t = json.loads(base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)))
        if set(t) != {'v', 'kind', 'key', 'digest', 'gateway', 'target'} or t['v'] != 1 or t['kind'] not in ('crawl', 'upload', 'refine'):
            raise ValueError()
        identifier(t['key'])
        if any(not re.fullmatch('[0-9a-f]{64}', t[k]) for k in ('digest', 'gateway')):
            raise ValueError()
        if t['kind'] == 'refine':
            identifier(t['target'])
        elif t['target'] != '':
            raise ValueError()
        return t
    except (ValueError, TypeError, KeyError):
        raise ValueError('복구 정보가 손상되었습니다. 기존 요청 기록을 확인하세요.') from None


def match(t, kind, body, base, file=None):
    if (t['kind'] != kind or t['digest'] != fingerprint(kind, body, file) or
            t['gateway'] != hashlib.sha256(base.encode()).hexdigest()):
        raise ValueError('보관된 요청과 입력 또는 서버 연결 설정이 일치하지 않습니다. 작업 현황을 확인하거나 새 요청을 작성하세요.')


STATUS = {'created': '준비 중', 'queued': '대기 중', 'running': '처리 중', 'succeeded': '완료', 'failed': '완료하지 못함',
          'cancelled': '취소됨', 'ready': '정리 완료', 'needs_review': '내용 확인 필요', 'needs_ocr': '일부 내용 미확인',
          'pending': '준비 중', 'delivered': '요청 접수됨', 'blocked': '진행 확인 필요'}
PHASE = {'submission_pending': '시작을 준비하고 있어요', 'collecting_sources': '자료를 가져오고 있어요', 'rendering_pages': '웹페이지를 읽고 있어요',
         'reading_source': '파일을 열고 있어요', 'parsing_and_cleaning': '내용을 정리하고 있어요', 'parsing_and_ocr': '글자와 표를 읽고 있어요',
         'uploading_canonical': '결과를 저장하고 있어요', 'verifying_canonical': '저장한 결과를 확인하고 있어요',
         'canonical_registered': '마무리하고 있어요', 'completed': '완료', 'failed': '완료하지 못했어요',
         'downloading_source': '자료를 가져오고 있어요', 'verifying_sources': '가져온 자료를 확인하고 있어요'}


def label(value):
    return STATUS.get(value, '확인 중')


def row_state(row):
    if row.get('purge_state'):
        return '파일 삭제됨' if row.get('purged_at') else '파일 삭제 중'
    if row.get('deleted_at'):
        return '목록에서 삭제됨'
    if row.get('status'):
        return label(row['status'])
    if row.get('state'):
        if row['state'] == 'ready': return '가져오기 완료'
        return label(row['state'])
    if row.get('output_document_id') or row.get('output_document_ids'):
        return '결과 있음'
    return label(row.get('submission_status'))


# Deterministic extensions from registered metadata; never guess from a URL.
FORMAT_EXTENSIONS = {'text': '.txt', 'markdown': '.md', 'html': '.html', 'pdf': '.pdf',
                     'png': '.png', 'jpeg': '.jpg', 'docx': '.docx', 'xlsx': '.xlsx'}
MIME_EXTENSIONS = {
    'text/plain': '.txt', 'text/markdown': '.md', 'text/x-markdown': '.md',
    'text/html': '.html', 'application/xhtml+xml': '.html', 'application/pdf': '.pdf',
    'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
    'application/json': '.json', 'text/csv': '.csv', 'application/xml': '.xml',
    'text/xml': '.xml', 'application/zip': '.zip',
}


def download_name(collection, id, metadata, representation='content'):
    """Name verified bytes without converting them or changing their MIME policy."""
    identifier(id)
    if collection == 'canonical-documents':
        return 'canonical-' + id + '.json'
    mime = str(metadata.get('content_type') or '').split(';', 1)[0].strip().lower()
    if representation == 'http-response':
        return 'http-response-' + id + MIME_EXTENSIONS.get(mime, '.bin')
    original = metadata.get('original_filename')
    if isinstance(original, str) and original:
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', original).strip(' .')
        if name:
            stem, dot, suffix = name.rpartition('.')
            if dot and 0 < len(suffix) <= 12:
                return stem[:219 - len(suffix)] + '.' + suffix
            return name[:220]
    if (metadata.get('browser_capture') or {}).get('representation') == 'rendered_dom':
        extension = '.html'
    else:
        extension = FORMAT_EXTENSIONS.get(str(metadata.get('format') or '').lower()) or MIME_EXTENSIONS.get(mime, '.bin')
    return 'source-' + id + extension


def source_kind(metadata):
    if (metadata.get('browser_capture') or {}).get('representation') == 'rendered_dom':
        return 'html'
    value = FORMAT_EXTENSIONS.get(str(metadata.get('format') or '').lower())
    mime = str(metadata.get('content_type') or '').split(';', 1)[0].strip().lower()
    return (value or MIME_EXTENSIONS.get(mime, '.bin')).lstrip('.')


def source_name(metadata):
    from urllib.parse import unquote, urlsplit
    original = metadata.get('original_filename')
    if original:
        return str(original)[:160]
    url = str(metadata.get('final_url') or metadata.get('source_url') or '')
    try:
        parts = urlsplit(url)
        name = unquote(parts.path.rstrip('/').rsplit('/', 1)[-1]) or parts.hostname
    except ValueError:
        name = ''
    return str(name or metadata.get('name') or '수집한 파일')[:160]


def source_type_label(metadata):
    kind = source_kind(metadata)
    return {'html': '웹페이지 · HTML', 'pdf': 'PDF 문서', 'png': '이미지 · PNG',
            'jpg': '이미지 · JPEG', 'gif': '이미지 · GIF', 'webp': '이미지 · WebP',
            'docx': 'Word 문서', 'xlsx': 'Excel 문서', 'txt': '텍스트', 'md': 'Markdown',
            'json': 'JSON', 'xml': 'XML', 'csv': 'CSV'}.get(kind, '파일')


def file_size(value):
    size = int(value or 0)
    if size < 1024: return str(size) + ' B'
    if size < 1024 * 1024: return f'{size / 1024:.1f} KB'
    return f'{size / (1024 * 1024):.1f} MB'


def refinement_body(metadata, deployment=''):
    """Choose recognition only for the formats accepted by the backend."""
    body = {'name': (source_name(metadata) + ' 내용 정리')[:200],
            'document_id': identifier(metadata.get('document_id')), 'rules_version': 'basic-cleaning-v1'}
    if source_kind(metadata) in ('pdf', 'png', 'jpg') and deployment.strip():
        body['ocr_deployment_id'] = identifier(deployment.strip())
    return body


def ocr_deployment_ready(value, deployment):
    """Public observed state only; the backend still verifies freshness/identity."""
    serving = value.get('serving')
    return (value.get('deployment_id') == deployment and isinstance(serving, dict)
            and serving.get('engine') == 'vllm' and serving.get('profile') == 'surya-ocr-v2'
            and value.get('artifact_kind') == 'snapshot' and value.get('status') == 'ready'
            and value.get('runtime_state') == 'ready' and value.get('desired_state') == 'running')


def collection_body(url, attachments=True, follow=False, limit=10):
    """Bounded browser collection; users choose scope, not an extraction engine."""
    return {'name': (url.strip() + ' 수집')[:200], 'url': url.strip(), 'options': {
        'render_javascript': True, 'render_wait_ms': 1000,
        'follow_links': bool(follow), 'include_attachments': bool(attachments),
        'max_documents': int(limit), 'max_depth': 1 if follow else 0,
        'max_bytes': 8 << 20, 'max_total_bytes': 32 << 20, 'timeout_seconds': 120}}


def friendly_error(value):
    code = str(getattr(value, 'code', ''))
    raw = str(value)
    if 'PDF signature was not found' in raw:
        return 'PDF 형식으로 식별할 수 없는 파일입니다. 선택한 파일의 형식을 확인해주세요.'
    if 'file structure does not match its extension' in raw:
        return '서버의 파일 형식 검사를 통과하지 못했습니다. 파일 형식을 확인하거나 아래 문제 해결 정보를 전달해주세요.'
    if any(term in raw for term in ('file exceeds the 32 MiB', 'file must contain 1..33554432', 'upload size must be 1..33554432', 'upload body exceeds the request limit')):
        return '파일이 비어 있거나 업로드 용량 한도를 넘었습니다. 32 MiB 이하의 파일을 선택해주세요.'
    if 'text uploads must use UTF-8' in raw:
        return '텍스트 파일의 문자 인코딩을 읽을 수 없습니다. UTF-8로 저장한 파일을 선택해주세요.'
    if 'filename must be a basename' in raw or 'filename contains control characters' in raw:
        return '파일 이름이 너무 길거나 사용할 수 없는 문자가 있습니다. 파일 이름을 짧고 간단하게 바꾼 뒤 선택해주세요.'
    if 'unsupported file extension' in raw:
        return '지원하지 않는 파일 형식입니다. PDF, Word(DOCX), Excel(XLSX), TXT, Markdown, HTML, PNG 또는 JPG 파일을 선택해주세요.'
    if 'macro-enabled files are unsupported' in raw:
        return '매크로가 포함된 문서는 가져올 수 없습니다. 매크로가 없는 DOCX 또는 XLSX로 저장해주세요.'
    if 'invalid DOCX/XLSX package' in raw:
        return 'Word 또는 Excel 파일의 내부 구조를 읽지 못했습니다. 원본 프로그램에서 DOCX 또는 XLSX로 다시 저장해주세요.'
    if 'uploaded bytes do not match declared size' in raw:
        return '전송된 파일이 원래 파일 정보와 일치하지 않습니다. 파일을 다시 선택해주세요.'
    if 'multipart' in raw or 'exactly one name field and one file field' in raw:
        return '서버가 파일 전송 형식을 처리하지 못했습니다. 아래 문제 해결 정보를 전달해주세요.'
    if code == 'ResourceExhausted' or 'ResourceExhausted:' in raw:
        return '현재 다른 파일을 처리하고 있습니다. 잠시 후 요청을 다시 확인해주세요.'
    if code == 'INTEGRITY_ERROR' or 'SHA-256' in raw:
        return '파일을 확인하는 중 문제가 발생했습니다. 다시 불러오거나 관리자에게 문의해주세요.'
    if 'OCR deployment' in raw or 'REFINEMENT_OCR_UNAVAILABLE' in raw:
        return '글자 인식 모델을 현재 사용할 수 없습니다. 화면 위에서 OCR 모델을 다시 조회하고 선택해주세요.'
    if 'REFINEMENT_PARSE_FAILED' in raw:
        return '이 파일의 내용을 읽지 못했습니다. 원본이 정상적으로 열리는지 확인해주세요.'
    if '404' in raw or getattr(value, 'status', 0) == 404:
        return '자료를 찾을 수 없습니다. 목록을 새로고침해주세요.'
    if '공개 HTTP(S)' in raw:
        return '웹 주소를 확인해주세요. https://로 시작하는 주소를 입력하세요.'
    if '파일 이름과 크기' in raw:
        return '파일을 선택해주세요. 비어 있지 않은 32 MB 이하의 파일을 가져올 수 있습니다.'
    if code == 'CONNECTION_ERROR' or 'CONNECTION_ERROR' in raw:
        return '서버 연결이 끊겼습니다. 잠시 후 요청을 다시 확인해주세요.'
    return '처리를 완료하지 못했습니다. 입력한 자료를 확인하거나 잠시 후 다시 시도해주세요.'
