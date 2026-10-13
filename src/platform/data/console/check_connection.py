"""Read-only server verification from the UI container; never submits work."""
import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.request

from data_api import API, APIError


def check_refinement(api, run_id, *, expected_ocr_pages=None, expected_source_sha256=None):
    """Verify stored results and provenance through GET/download only.

    An OCR page expectation refers to all pages of an OCR-only input, such as
    the two-page sample PDF. Omit it for mixed native-text/OCR documents.
    """
    run = api.get('refinement-runs', run_id)
    if run.get('refinement_run_id') != run_id or run.get('status') != 'succeeded' or not run.get('output_document_id'):
        raise APIError('지정한 내용 정리 작업이 성공으로 완료되지 않았습니다.')
    source_id, document_id = run['source_document_id'], run['output_document_id']
    source = api.get('source-documents', source_id)
    api.download('source-documents', source_id, source)
    meta = api.get('canonical-documents', document_id)
    document = json.loads(api.download('canonical-documents', document_id, meta))
    if (source.get('document_id') != source_id or meta.get('document_id') != document_id
            or meta.get('source_document_id') != source_id or meta.get('refinement_run_id') != run_id
            or run.get('source_sha256') != source.get('sha256')
            or document.get('source') != {'document_id': source_id, 'sha256': source.get('sha256'), 'format': run.get('source_format')}):
        raise APIError('원본과 정리 결과의 식별자 또는 해시가 일치하지 않습니다.')
    if expected_source_sha256 is not None and source.get('sha256') != expected_source_sha256:
        raise APIError('확인하려는 원본 파일의 SHA-256과 다릅니다.')
    pages, blocks = document['pages'], document['blocks']
    if (document.get('schema_version') != 'canonical-document-v1'
            or meta.get('schema_version') != document['schema_version']
            or not isinstance(pages, list) or not isinstance(blocks, list)
            or meta.get('page_count') != len(pages) or meta.get('block_count') != len(blocks)):
        raise APIError('정리 결과의 구조 또는 페이지·블록 개수가 일치하지 않습니다.')
    quality = document['quality']
    expected_state = ('needs_ocr' if any(i['requires_ocr'] for i in quality['issues'])
                      else 'needs_review' if quality['issues'] else 'ready')
    if quality.get('state') != expected_state or meta.get('state') != expected_state:
        raise APIError('정리 결과의 품질 상태가 일치하지 않습니다.')
    processor = document['processor']
    version = 'cpu-parser-surya-v1' if run.get('ocr') else 'cpu-parser-v1'
    if (processor.get('version') != version or meta.get('processor_version') != version
            or not processor.get('image_id') or processor['image_id'] != meta.get('processor_image_id')):
        raise APIError('정리 결과의 처리기 정보가 일치하지 않습니다.')
    ocr = document.get('ocr') or {}
    if (run.get('ocr') and ocr.get('identity') != run['ocr']) or (not run.get('ocr') and document.get('ocr') is not None):
        raise APIError('정리 요청과 결과의 글자 인식 모델 정보가 일치하지 않습니다.')
    records = ocr.get('pages', [])
    if expected_ocr_pages is not None:
        expected_numbers = list(range(1, expected_ocr_pages + 1))
        if (not run.get('ocr') or expected_state == 'needs_ocr'
                or len(pages) != expected_ocr_pages or len(records) != expected_ocr_pages
                or sorted(p['number'] for p in pages) != expected_numbers
                or sorted(p['page'] for p in records) != expected_numbers
                or any(p.get('status') != 'completed' for p in records)):
            raise APIError('모든 페이지의 글자 인식 완료를 확인하지 못했습니다.')
        for number in expected_numbers:
            readable = [b for b in blocks if b.get('location', {}).get('page') == number
                        and (str(b.get('text') or '').strip() or any(
                            str(c.get('text') or c.get('value') or '').strip() for c in b.get('cells', [])))]
            if not readable:
                raise APIError('글자 인식 완료 페이지에 읽을 수 있는 본문·표가 없습니다.')
    return {
        'refinement_run_id': run_id, 'document_id': document_id,
        'source_document_id': source_id, 'source_sha256': source['sha256'],
        'state': meta['state'], 'page_count': len(pages), 'block_count': len(blocks),
        'size_bytes': meta['size_bytes'], 'sha256': meta['sha256'],
        'ocr_completed_pages': sum(p.get('status') == 'completed' for p in records),
        'ocr_partial_pages': sum(p.get('status') == 'partial' for p in records),
        'ocr_failed_pages': sum(p.get('status') == 'failed' for p in records),
        'ocr_skipped_pages': sum(p.get('status') == 'skipped' for p in records),
        'expected_ocr_pages': expected_ocr_pages,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-id')
    p.add_argument('--refinement-run-id')
    p.add_argument('--expect-ocr-pages', type=int, help='전체 페이지를 OCR하는 자료의 기대 페이지 수')
    p.add_argument('--expect-source-sha256', help='확인할 원본의 SHA-256')
    args = p.parse_args()
    if (args.expect_ocr_pages is not None or args.expect_source_sha256 is not None) and not args.refinement_run_id:
        p.error('OCR·원본 기대값에는 --refinement-run-id가 필요합니다.')
    if args.expect_ocr_pages is not None and args.expect_ocr_pages < 1:
        p.error('--expect-ocr-pages는 1 이상이어야 합니다.')
    if args.expect_source_sha256 is not None and not re.fullmatch('[a-f0-9]{64}', args.expect_source_sha256):
        p.error('--expect-source-sha256에는 소문자 SHA-256을 입력하세요.')
    # Health may take a few seconds immediately after compose up.
    healthy = False
    for _ in range(20):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=2) as r:
                healthy = r.status == 200 and r.read(32).strip() == b'ok'
        except (urllib.error.URLError, OSError):
            pass
        if healthy:
            break
        time.sleep(1)
    if not healthy:
        raise APIError('Streamlit health가 준비되지 않았습니다.')
    print('PASS: Streamlit health')
    api = API()
    for collection in ('crawl-runs', 'source-documents', 'refinement-runs', 'operations'):
        result = api.listing(collection, limit=1)
        print('[list]', json.dumps({'collection': collection, 'returned': len(result['items']), 'has_next': bool(result.get('next_cursor'))}))
    if args.source_id:
        meta = api.get('source-documents', args.source_id)
        raw = api.download('source-documents', args.source_id, meta)
        print('[source]', json.dumps({'document_id': args.source_id, 'size_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}))
        c = meta.get('browser_capture')
        if c:
            raw = api.download('source-documents', args.source_id, {'size_bytes': c['response_size_bytes'], 'sha256': c['response_sha256']}, 'http-response')
            print('[http-response]', json.dumps({'size_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}))
    if args.refinement_run_id:
        result = check_refinement(api, args.refinement_run_id,
                                  expected_ocr_pages=args.expect_ocr_pages,
                                  expected_source_sha256=args.expect_source_sha256)
        print('[canonical]', json.dumps(result))
    print('PASS: console -> existing REST APIs; verified requested downloads; zero POST/Jobs')


if __name__ == '__main__':
    try:
        main()
    except (APIError, ValueError) as e:
        raise SystemExit('FAIL: ' + str(e)) from None
    except (KeyError, TypeError, AttributeError):
        raise SystemExit('FAIL: 조회한 결과의 필수 필드 또는 형식이 올바르지 않습니다.') from None
