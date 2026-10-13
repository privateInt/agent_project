#!/usr/bin/env python3
"""Source document -> gated CPU parsing -> hash-verified canonical JSON.

No OCR/model deployment, source deletion or service reset. A saved report is the
replay identity: after a lost reply use --resume instead of starting a new run.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import urllib.parse
import uuid
from live_collection_smoke import API, expect, save
from live_upload_smoke import make_report as upload_report, checked_source, upload


def make_report(api, document_ids):
    key = str(uuid.uuid4())
    items = []
    if document_ids:
        items = [{'source_document_id': str(uuid.UUID(value))} for value in document_ids]
    else:
        items = [{'upload': item} for item in upload_report(api, [])['uploads']]
    for i, item in enumerate(items):
        item.update({'key': key + '-' + str(i), 'name': 'refinement-smoke-' + key + '-' + str(i)})
    return {'schema_version': 1, 'kind': 'refinement-smoke', 'api': api.base, 'idempotency_key': key, 'items': items, 'passed': False}


def verify_result(source, run, metadata, content):
    if run.get('status') != 'succeeded' or not run.get('output_document_id'):
        raise ValueError('Refinement did not finish')
    if metadata.get('document_id') != run['output_document_id'] or metadata.get('refinement_run_id') != run['refinement_run_id'] or metadata.get('source_document_id') != source['document_id']:
        raise ValueError('Canonical document identity mismatch')
    if metadata.get('size_bytes') != len(content) or metadata.get('sha256') != hashlib.sha256(content).hexdigest():
        raise ValueError('Canonical download checksum mismatch')
    document = json.loads(content)
    if document.get('schema_version') != 'canonical-document-v1' or metadata.get('schema_version') != document['schema_version']:
        raise ValueError('Unknown canonical schema')
    expected_source = {'document_id': source['document_id'], 'sha256': source['sha256'], 'format': run['source_format']}
    if document.get('source') != expected_source or run.get('source_sha256') != source['sha256']:
        raise ValueError('Canonical source provenance mismatch')
    processor = document['processor']
    expected_processor = 'cpu-parser-surya-v1' if run.get('ocr') else 'cpu-parser-v1'
    if run.get('ocr') and document.get('ocr', {}).get('identity') != run['ocr']:
        raise ValueError('Pinned OCR model provenance mismatch')
    if not run.get('ocr') and document.get('ocr') is not None:
        raise ValueError('Unexpected OCR inference')
    if processor.get('version') != expected_processor or metadata.get('processor_version') != expected_processor or processor.get('rules_version') != 'basic-cleaning-v1' or processor.get('image_id') != metadata.get('processor_image_id'):
        raise ValueError('Canonical processor provenance mismatch')
    quality = document['quality']
    expected = 'needs_ocr' if any(i['requires_ocr'] for i in quality['issues']) else ('needs_review' if quality['issues'] else 'ready')
    if quality.get('state') != expected or metadata.get('state') != expected:
        raise ValueError('Canonical quality state mismatch')
    if metadata.get('block_count') != len(document['blocks']) or metadata.get('page_count') != len(document['pages']):
        raise ValueError('Canonical structure counts mismatch')
    for order, block in enumerate(document['blocks']):
        if block.get('order') != order or block.get('id') != 'b' + str(order + 1):
            raise ValueError('Canonical block order mismatch')
    return document


def runtimes(operation_id, run_id):
    ids = subprocess.run(['docker', 'ps', '-aq', '--filter', 'label=data-platform.owner=dispatcher', '--filter', 'label=data-platform.operation_id=' + operation_id], check=True, capture_output=True, text=True, timeout=15).stdout.split()
    if not ids:
        return []
    values = json.loads(subprocess.run(['docker', 'inspect', *ids], check=True, capture_output=True, text=True, timeout=15).stdout)
    results = []
    for value in values:
        labels = value.get('Config', {}).get('Labels', {})
        host = value.get('HostConfig', {})
        if labels.get('data-platform.execution_type') != 'refinement.parse' or labels.get('data-platform.resource_id') != run_id or labels.get('data-platform.operation_id') != operation_id:
            raise ValueError('Refinement runtime identity mismatch')
        if host.get('DeviceRequests') or host.get('Privileged') or not host.get('ReadonlyRootfs') or host.get('RestartPolicy', {}).get('Name') != 'no':
            raise ValueError('Refinement runtime is not a bounded CPU one-shot Job')
        results.append({'id': value['Id'], 'image_id': value['Image'], 'state': value['State']['Status'], 'running': value['State']['Running'], 'exit_code': value['State']['ExitCode']})
    return results


def execute(api, report, path, timeout):
    for item in report['items']:
        if 'upload' in item:
            source = checked_source(item['upload'])
            code, uploaded = upload(api, item['upload'], source)
            expect(code, 200, uploaded)
            if item.get('source_document_id') and item['source_document_id'] != uploaded['document_id']:
                raise ValueError('Source upload identity changed during resume')
            item['source_document_id'] = uploaded['document_id']
            save(path, report)
        source_id = item['source_document_id']
        code, source = api.call('/api/source-documents/' + source_id)
        expect(code, 200, source)
        if item.get('source') and item['source'] != source:
            raise ValueError('Previously recorded immutable source changed')
        item['source'] = source
        request = {'name': item['name'], 'document_id': source_id, 'rules_version': 'basic-cleaning-v1'}
        if report.get('ocr_deployment_id'):
            request['ocr_deployment_id'] = report['ocr_deployment_id']
        item['request'] = request
        save(path, report)  # Before POST, including a lost acceptance reply.
        code, accepted = api.call('/api/refinement-runs', request, item['key'])
        expect(code, 202, accepted)
        if item.get('accepted') and item['accepted'] != accepted:
            raise ValueError('Resumed refinement changed its identity')
        item['accepted'] = accepted
        save(path, report)
        code, replay = api.call('/api/refinement-runs', request, item['key'])
        expect(code, 202, replay)
        if replay != accepted:
            raise ValueError('Replay duplicated refinement')
        code, conflict = api.call('/api/refinement-runs', dict(request, name='changed-' + item['name']), item['key'])
        expect(code, 409, conflict)
        deadline = time.monotonic() + timeout
        previous = None
        while time.monotonic() < deadline:
            code, run = api.call('/api/refinement-runs/' + accepted['refinement_run_id'])
            expect(code, 200, run)
            item['last_run'] = run
            state = {k: run.get(k) for k in ('status', 'phase', 'submission_status', 'error_code')}
            if state != previous:
                print('[refinement]', json.dumps(state), flush=True)
                previous = state
            save(path, report)
            if run.get('status') in ('failed', 'cancelled') or run.get('submission_status') == 'blocked':
                raise ValueError('Refinement failed or submission blocked: ' + json.dumps(state))
            if run.get('status') == 'succeeded':
                break
            time.sleep(2)
        else:
            raise ValueError('Refinement timed out; resume using the saved report')
        doc_id = run['output_document_id']
        code, metadata = api.call('/api/canonical-documents/' + doc_id)
        expect(code, 200, metadata)
        code, content, headers = api.call('/api/canonical-documents/' + doc_id + '/content', binary=True, timeout=90)
        expect(code, 200, 'canonical content')
        document = verify_result(source, run, metadata, content)
        if not headers.get('Content-Disposition', '').startswith('attachment;') or headers.get('X-Content-Type-Options') != 'nosniff':
            raise ValueError('Canonical attachment headers missing')
        query = urllib.parse.urlencode({'resource_type': 'refinement_run', 'resource_id': accepted['refinement_run_id']})
        code, operations = api.call('/api/operations?' + query)
        expect(code, 200, operations)
        if len(operations['items']) != 1 or operations['items'][0].get('operation_id') != accepted['operation_id'] or operations['items'][0].get('status') != 'succeeded':
            raise ValueError('Refinement has duplicate or unfinished operations')
        code, listed = api.call('/api/refinement-runs?' + urllib.parse.urlencode({'q': item['name'], 'limit': 2}))
        expect(code, 200, listed)
        if len(listed['items']) != 1 or listed['items'][0].get('refinement_run_id') != accepted['refinement_run_id']:
            raise ValueError('Refinement list has missing or duplicate request')
        # Verify source bytes were preserved by this derived operation.
        code, original, _ = api.call('/api/source-documents/' + source_id + '/content', binary=True, timeout=90)
        expect(code, 200, 'original content')
        if len(original) != source['size_bytes'] or hashlib.sha256(original).hexdigest() != source['sha256']:
            raise ValueError('Original source changed')
        stop_deadline = time.monotonic() + 30
        observed = []
        while time.monotonic() < stop_deadline:
            observed = runtimes(accepted['operation_id'], accepted['refinement_run_id'])
            if observed and all(not r['running'] for r in observed):
                break
            time.sleep(1)
        if not observed or any(r['running'] for r in observed) or not any(r['state'] == 'exited' and r['image_id'] == metadata['processor_image_id'] and r['exit_code'] == 0 for r in observed):
            raise ValueError('Authorized CPU job exit/image not confirmed')
        item.update({'canonical': metadata, 'quality': document['quality'], 'runtimes': observed, 'passed': True})
        save(path, report)
        print('[canonical]', json.dumps(metadata, ensure_ascii=False), flush=True)
    report['passed'] = True
    save(path, report)
    print('PASS: immutable source -> gated CPU job -> verified canonical JSON; OCR requirement retained.')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--api', default='http://127.0.0.1:14000')
    p.add_argument('--document-id', action='append', default=[])
    p.add_argument('--resume', type=Path)
    p.add_argument('--timeout', type=int, default=720)
    args = p.parse_args()
    if args.timeout < 1:
        p.error('--timeout must be positive')
    if args.resume and args.document_id:
        p.error('--resume uses the original recorded document IDs')
    if args.resume:
        path = args.resume.resolve()
        report = json.loads(path.read_text(encoding='utf-8'))
        if report.get('kind') != 'refinement-smoke' or report.get('schema_version') != 1:
            raise ValueError('Unsupported refinement report')
        api = API(report['api'])
    else:
        api = API(args.api)
        report = make_report(api, args.document_id)
        path = Path(__file__).resolve().parent / 'results' / 'refinement' / (report['idempotency_key'] + '.json')
    report['passed'] = False
    save(path, report)
    print('[record]', path, flush=True)
    deadline = time.monotonic() + 120
    while True:
        try:
            code, _ = api.call('/api/refinement-runs?limit=1', timeout=5)
            if code == 200: break
        except (OSError, ValueError): pass
        if time.monotonic() >= deadline:
            raise ValueError('Refinement API not ready; no request submitted')
        time.sleep(2)
    execute(api, report, path, args.timeout)

if __name__ == '__main__':
    try: main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit('FAIL: ' + str(exc))
