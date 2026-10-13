#!/usr/bin/env python3
"""Prepare a managed Surya deployment and verify PNG/scanned-PDF OCR.

Uses Python's standard library and Docker. Every create key is saved before
POST; resume the printed report after a lost reply. Keeps the OCR deployment
running for later refinement; never stops unrelated workloads or resets data.
"""
import argparse
import getpass
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from live_collection_smoke import API, expect, save
from live_upload_smoke import make_report as upload_report
from live_refinement_smoke import make_report as refinement_report, execute as refine

REPOSITORY = 'datalab-to/surya-ocr-2'
REVISION = '3b3d4cdf88d6928b0acdc75181b13206ea67c4a3'
PROFILE = 'surya-ocr-v2'
ROOT = Path(__file__).resolve().parent


def post(api, report, path, name, endpoint, body, expected, hf_token=''):
    if 'hf_token' in body or (hf_token and name != 'import'):
        raise ValueError('Credentials must use the transient import argument')
    record = report.setdefault(name, {'key': report['key'] + '-' + name, 'endpoint': endpoint, 'request': body})
    if record['endpoint'] != endpoint or record['request'] != body:
        raise ValueError('Recorded request changed: ' + name)
    if name == 'import':
        # A received acceptance is durable; follow its version by GET. This
        # also lets resume work after Management deleted the transient token.
        if record.get('accepted') is not None:
            return record['accepted']
        required = record.setdefault('requires_hf_token', bool(hf_token))
        if required != bool(hf_token):
            raise ValueError('Resume must use the original token mode; use --hf-token for a recorded authenticated import')
    save(path, report)
    request = dict(body, hf_token=hf_token) if hf_token else body
    code, accepted = api.call(endpoint, request, record['key'], timeout=40)
    expect(code, expected, accepted)
    if record.get('accepted') is not None and record['accepted'] != accepted:
        raise ValueError('Replay changed resource identity: ' + name)
    record['accepted'] = accepted
    save(path, report)
    return accepted


def wait(api, endpoint, report, path, label, timeout, ready):
    deadline, previous = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            code, value = api.call(endpoint, timeout=15)
        except (OSError, ValueError):
            time.sleep(2)
            continue
        if code >= 500:
            time.sleep(2)
            continue
        expect(code, 200, value)
        report[label + '_last'] = value
        save(path, report)
        state = {k: value[k] for k in ('state', 'status', 'phase', 'submission_status', 'runtime_state', 'runtime_error_code') if k in value}
        if state != previous:
            print('[' + label + ']', json.dumps(state), flush=True)
            previous = state
        if ready(value):
            return value
        if value.get('state') == 'failed' or value.get('status') in ('failed', 'stopped') or value.get('submission_status') == 'blocked':
            if value.get('operation_id'):
                try:
                    code, detail = api.call('/api/operations/' + value['operation_id'], timeout=15)
                    if code == 200:
                        failure = {k: detail.get(k) for k in ('operation_id', 'status', 'phase', 'error_code', 'error_message')}
                        report[label + '_operation'] = failure
                        save(path, report)
                        print('[operation]', json.dumps(failure), flush=True)
                except (OSError, ValueError):
                    pass
            raise ValueError(label + ' failed; inspect the saved report and its Operation/runtime logs')
        time.sleep(2)
    raise ValueError(label + ' timed out; resume the saved report (a waiting GPU is not a failed deployment)')


def verify_ocr_document(document, expected_identity):
    result = document.get('ocr') or {}
    if result.get('identity') != expected_identity or result.get('client_version') != 'surya-openai-v1':
        raise ValueError('OCR provenance mismatch')
    pages = result.get('pages') or []
    if len(pages) != 1 or pages[0].get('status') != 'completed':
        raise ValueError('OCR did not complete the fixture page: ' + json.dumps([{k: p.get(k) for k in ('page', 'status', 'reason')} for p in pages]))
    text = ' '.join(b.get('text', '') for b in document['blocks'])
    normalized = re.sub(r'[^A-Z0-9]', '', text.upper())
    if 'OCRTEST12345' not in normalized:
        raise ValueError('Fixture text was not recognized; inspect the saved canonical JSON')
    if document['quality']['state'] != 'needs_review' or any(i['requires_ocr'] for i in document['quality']['issues']):
        raise ValueError('Fixture OCR remains incomplete or hid the review requirement')
    page = document['pages'][0]
    for block in document['blocks']:
        box = block.get('location', {}).get('bbox', [])
        if (len(box) != 4 or not 0 <= box[0] < box[2] <= page['width']
                or not 0 <= box[1] < box[3] <= page['height']):
            raise ValueError('OCR coordinates are outside the source page')
    return {'recognized_fixture_text': True, 'block_count': len(document['blocks']),
            'quality': document['quality'], 'pages': [{'page': p['page'], 'status': p['status']} for p in pages]}


def retry_import_report(previous, api, key, gpu_id):
    if previous.get('schema_version') != 1 or previous.get('kind') != 'surya-ocr-smoke':
        raise ValueError('Unsupported OCR report')
    imported = previous['import']
    request, accepted = imported['request'], imported['accepted']
    if request.get('repo_id') != REPOSITORY or request.get('revision') != REVISION:
        raise ValueError('Previous import is not the pinned Surya snapshot')
    code, version = api.call('/api/model-versions/' + accepted['model_version_id'])
    expect(code, 200, version)
    if (version.get('state') != 'failed' or version.get('model_version_id') != accepted['model_version_id']
            or version.get('operation_id') != accepted['operation_id'] or version.get('repo_id') != REPOSITORY
            or version.get('requested_revision') != REVISION):
        raise ValueError('Only a confirmed failed Surya import may start a replacement import')
    code, operation = api.call('/api/operations/' + accepted['operation_id'])
    expect(code, 200, operation)
    if operation.get('status') != 'failed' or operation.get('operation_id') != accepted['operation_id']:
        raise ValueError('Previous import Operation is not confirmed failed')
    model_id = previous.get('model_id') or previous['model']['accepted']['model_id']
    if version.get('model_id') != model_id:
        raise ValueError('Previous model identity changed')
    code, model = api.call('/api/models/' + model_id)
    expect(code, 200, model)
    if model.get('model_id') != model_id or model.get('model_type') != 'ocr':
        raise ValueError('Previous OCR model is unavailable')
    return {'schema_version': 1, 'kind': 'surya-ocr-smoke', 'key': key,
            'model_api': api.base, 'data_api': previous['data_api'], 'gpu_id': gpu_id,
            'passed': False, 'model_id': model_id, 'import_version': 'surya-2-' + REVISION[:12] + '-' + key,
            'retry_of': {'model_version_id': accepted['model_version_id'], 'operation_id': accepted['operation_id'],
                         'report_key': previous['key']}}


def execute(report, path, hf_token=''):
    model, data = API(report['model_api']), API(report['data_api'])
    # Compose can return before the gateways and their gRPC dependencies are ready.
    # Poll only reads; create requests still have no automatic HTTP retry.
    wait(model, '/api/models?limit=1', report, path, 'model_api', 120,
         lambda value: isinstance(value.get('items'), list))
    wait(data, '/api/refinement-runs?limit=1', report, path, 'data_api', 120,
         lambda value: isinstance(value.get('items'), list))
    supplied = report.get('deployment_id')
    if not supplied:
        model_id = report.get('model_id')
        if not model_id:
            registered = post(model, report, path, 'model', '/api/models',
                              {'name': 'surya-ocr-' + report['key'], 'model_type': 'ocr', 'description': 'Managed Surya OCR v2'}, 201)
            model_id = registered['model_id']
        imported = post(model, report, path, 'import', '/api/models/' + model_id + '/imports/huggingface',
                        {'version': report.get('import_version', 'surya-2-' + REVISION[:12]),
                         'repo_id': REPOSITORY, 'revision': REVISION}, 202, hf_token=hf_token)
        version = wait(model, '/api/model-versions/' + imported['model_version_id'], report, path, 'model_version', 2700,
                       lambda v: v.get('state') == 'ready')
        if version.get('repo_id') != REPOSITORY or version.get('resolved_revision') != REVISION or version.get('artifact_kind') != 'snapshot':
            raise ValueError('Imported OCR snapshot does not match the pinned revision')
        deployed = post(model, report, path, 'deployment', '/api/deployments',
                        {'model_version_id': version['model_version_id'], 'resources': {'gpu_ids': [report['gpu_id']]},
                         'serving': {'engine': 'vllm', 'profile': PROFILE, 'max_model_len': 18000, 'gpu_memory_utilization': 0.5}}, 202)
        supplied = deployed['deployment_id']
    dep = wait(model, '/api/deployments/' + supplied, report, path, 'serving', 1800,
               lambda v: v.get('status') == 'ready' and v.get('runtime_state') == 'ready' and v.get('desired_state') == 'running')
    if dep.get('serving', {}).get('profile') != PROFILE or dep.get('artifact_kind') != 'snapshot':
        raise ValueError('Selected deployment is not a ready Surya OCR snapshot')
    report['active_deployment_id'] = dep['deployment_id']
    save(path, report)
    print('[ocr-deployment]', json.dumps({k: dep.get(k) for k in ('deployment_id', 'model_version_id', 'runtime_image_id')}), flush=True)

    subpath = path.with_name(path.stem + '-refinement.json')
    if 'refinement_key' not in report:
        sub = refinement_report(data, [])
        uploads = upload_report(data, [ROOT / 'fixtures/ocr/ocr-smoke.png', ROOT / 'fixtures/ocr/ocr-smoke.pdf'])['uploads']
        sub['items'] = [dict(sub['items'][index], upload=item) for index, item in enumerate(uploads)]
        sub['ocr_deployment_id'] = dep['deployment_id']
        report['refinement_key'] = sub['idempotency_key']
        report['initial_refinement_report'] = sub
        save(path, report)
    if subpath.exists():
        sub = json.loads(subpath.read_text())
    else:
        sub = report['initial_refinement_report']
    if sub.get('idempotency_key') != report['refinement_key'] or sub.get('ocr_deployment_id') != dep['deployment_id']:
        raise ValueError('OCR resume report identity mismatch')
    save(subpath, sub)
    print('[refinement-record]', subpath, flush=True)
    refine(data, sub, subpath, 1500)
    summaries = []
    for index, item in enumerate(sub['items']):
        code, raw, _ = data.call('/api/canonical-documents/' + item['canonical']['document_id'] + '/content', binary=True, timeout=90)
        expect(code, 200, 'canonical JSON')
        document = json.loads(raw)
        # The file contains synthetic fixtures, including raw generated HTML as data.
        output = path.with_name(path.stem + '-canonical-' + str(index) + '.json')
        save(output, document)
        pinned = item['last_run']['ocr']
        if pinned.get('deployment_id') != dep['deployment_id'] or pinned.get('runtime_image_id') != dep['runtime_image_id']:
            raise ValueError('OCR result used a different runtime')
        summaries.append(verify_ocr_document(document, pinned))
        print('[ocr-result]', json.dumps({'document_id': item['canonical']['document_id'], **summaries[-1]}), flush=True)
    report.update({'passed': True, 'results': summaries})
    save(path, report)
    print('PASS: managed Surya deployment -> PNG and scanned PDF -> pinned OCR provenance, text, coordinates and review state.')
    print('OCR deployment remains running:', dep['deployment_id'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-api', default='http://127.0.0.1:13000')
    parser.add_argument('--data-api', default='http://127.0.0.1:14000')
    parser.add_argument('--gpu-id', type=int, default=0)
    parser.add_argument('--deployment-id')
    parser.add_argument('--resume', type=Path)
    parser.add_argument('--retry-import', type=Path, help='Create a new version under the model from a failed OCR report')
    parser.add_argument('--hf-token', action='store_true', help='Prompt for a token without saving it to the report or command line')
    args = parser.parse_args()
    if args.gpu_id < 0:
        parser.error('--gpu-id must be nonnegative')
    if sum(bool(value) for value in (args.resume, args.retry_import, args.deployment_id)) > 1:
        parser.error('--resume, --retry-import and --deployment-id are mutually exclusive')
    if args.deployment_id and args.hf_token:
        parser.error('--hf-token is only for model import')
    if args.resume:
        path = args.resume.resolve()
        report = json.loads(path.read_text())
        if report.get('schema_version') != 1 or report.get('kind') != 'surya-ocr-smoke':
            raise ValueError('Unsupported OCR report')
    elif args.retry_import:
        previous = json.loads(args.retry_import.read_text())
        report = retry_import_report(previous, API(previous['model_api']), str(uuid.uuid4()), args.gpu_id)
        path = ROOT / 'results/ocr' / (report['key'] + '.json')
    else:
        key = str(uuid.uuid4())
        report = {'schema_version': 1, 'kind': 'surya-ocr-smoke', 'key': key,
                  'model_api': API(args.model_api).base, 'data_api': API(args.data_api).base,
                  'gpu_id': args.gpu_id, 'passed': False}
        if args.deployment_id:
            report['deployment_id'] = str(uuid.UUID(args.deployment_id))
        path = ROOT / 'results/ocr' / (key + '.json')
    report['passed'] = False
    save(path, report)
    print('[record]', path, flush=True)
    hf_token = ''
    if args.hf_token and not report.get('import', {}).get('accepted'):
        if not sys.stdin.isatty():
            raise ValueError('--hf-token requires an interactive terminal')
        hf_token = getpass.getpass('Hugging Face token (not saved): ').strip()
        if not hf_token:
            raise ValueError('Empty token; run without --hf-token for anonymous import')
    execute(report, path, hf_token)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, EOFError, subprocess.SubprocessError) as exc:
        raise SystemExit('FAIL: ' + str(exc)) from None
