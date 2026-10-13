"""Q04a validates downloaded CPU canonical results; returns references, never text."""
from dataclasses import asdict
import hashlib
import math

from .contracts import (ContractError, MAX_BYTES, canonical_reference, digest, integer, strict_json)
from .intake import check, UploadError

RULES = 'basic-cleaning-v1'
PROCESSOR = 'cpu-parser-v1'
FORMATS = {'txt': 'text', 'md': 'markdown', 'markdown': 'markdown', 'pdf': 'pdf'}
TYPES = {'text', 'markdown', 'paragraph', 'heading', 'code', 'table', 'image', 'unhandled_structure'}


def checked_result(meta, raw, receipt, job, run):
    """Identity/bytes/provenance/structure first; valid partial output stays attention."""
    check(isinstance(meta, dict) and isinstance(raw, bytes), 'CANONICAL_RESPONSE_INVALID', 502)
    expected = {'document_id': run['output_document_id'], 'refinement_run_id': receipt.resource_id,
                'source_document_id': job['source_document_id'], 'schema_version': 'canonical-document-v1'}
    check(all(meta.get(k) == v for k, v in expected.items()), 'CANONICAL_IDENTITY_MISMATCH', 502)
    check(integer(meta.get('size_bytes'), 1, MAX_BYTES) and len(raw) == meta['size_bytes']
          and digest(meta.get('sha256')) and hashlib.sha256(raw).hexdigest() == meta['sha256'],
          'CANONICAL_BYTES_MISMATCH', 502)
    try:
        body = strict_json(raw)
    except ContractError:
        raise UploadError('CANONICAL_JSON_INVALID', 502) from None
    pending = [body]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, float):
            check(math.isfinite(value), 'CANONICAL_JSON_INVALID', 502)
    source = {'document_id': job['source_document_id'], 'sha256': job['sha256'],
              'format': FORMATS[job['filename'].rsplit('.', 1)[-1].lower()]}
    check(body.get('schema_version') == 'canonical-document-v1' and body.get('source') == source,
          'CANONICAL_SOURCE_MISMATCH', 502)
    processor = body.get('processor')
    # Processor image is an immutable sha256 ID, distinct from a content digest.
    check(isinstance(processor, dict) and processor.get('version') == meta.get('processor_version') == PROCESSOR
          and processor.get('rules_version') == meta.get('rules_version') == RULES
          and isinstance(processor.get('image_id'), str) and processor['image_id'].startswith('sha256:')
          and digest(processor['image_id'][7:]) and processor['image_id'] == meta.get('processor_image_id')
          and isinstance(body.get('cleaning'), dict) and body['cleaning'].get('version') == RULES,
          'CANONICAL_PROCESSOR_MISMATCH', 502)
    processing = body.get('processing')
    check(body.get('ocr') is None and run.get('ocr') is None and (processing is None or
          processing == {'version': 'refinement-processing-v1', 'mode': 'parser_only'}),
          'UNEXPECTED_OCR_RESULT', 502)
    blocks, pages, quality = body.get('blocks'), body.get('pages'), body.get('quality')
    check(isinstance(blocks, list) and len(blocks) <= 100000 and isinstance(pages, list) and len(pages) <= 300
          and integer(meta.get('block_count'), 0, 100000) and meta['block_count'] == len(blocks)
          and integer(meta.get('page_count'), 0, 300) and meta['page_count'] == len(pages),
          'CANONICAL_STRUCTURE_INVALID', 502)
    for index, block in enumerate(blocks):
        check(isinstance(block, dict) and block.get('id') == 'b' + str(index + 1)
              and type(block.get('order')) is int and block['order'] == index
              and isinstance(block.get('type'), str) and block['type'] in TYPES
              and ('text' not in block or isinstance(block['text'], str)), 'CANONICAL_STRUCTURE_INVALID', 502)
    check(isinstance(quality, dict) and isinstance(quality.get('issues'), list)
          and len(quality['issues']) <= 100000, 'CANONICAL_QUALITY_INVALID', 502)
    issues = quality['issues']
    check(all(isinstance(i, dict) and isinstance(i.get('code'), str) and bool(i['code'])
              and type(i.get('requires_ocr')) is bool for i in issues), 'CANONICAL_QUALITY_INVALID', 502)
    state = 'needs_ocr' if any(i['requires_ocr'] for i in issues) else 'needs_review' if issues else 'ready'
    check(quality.get('state') == meta.get('state') == state, 'CANONICAL_QUALITY_INVALID', 502)
    summary = {**expected, **{k: meta[k] for k in ('sha256','size_bytes','state','block_count','page_count',
                                                    'processor_version','rules_version','processor_image_id')},
               'issue_count': len(issues), 'requires_ocr': state == 'needs_ocr'}
    if state != 'ready':
        return summary, 'OCR_DEFERRED' if state == 'needs_ocr' else 'CANONICAL_REVIEW_REQUIRED'
    if not any(isinstance(b.get('text'), str) and b['text'].strip() for b in blocks):
        return summary, 'CANONICAL_EMPTY'
    try:
        # Retain the Q01 gate unchanged. Extra Q04a CPU provenance checks precede it.
        ref = canonical_reference(meta, raw, receipt, job['source_document_id'], job['sha256'], run['output_document_id'])
    except ContractError as exc:
        raise UploadError(str(exc), 502) from None
    check(asdict(ref)['canonical_id'] == summary['document_id'], 'CANONICAL_IDENTITY_MISMATCH', 502)
    return summary, None
