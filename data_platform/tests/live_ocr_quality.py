#!/usr/bin/env python3
"""Measure Korean OCR using an existing ready Surya deployment; never deploy a model.

Resume the printed report after interruption. Completed canonical files can be
evaluated offline with --evaluate; neither mode silently creates a replacement run.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import uuid

from live_collection_smoke import API, expect, save
from live_upload_smoke import make_report as upload_report, checked_source
from live_refinement_smoke import execute as refine, verify_result
from ocr_quality import RUBRIC, GEOMETRY_RUBRIC, SUPPORTED_RUBRICS, evaluate_case, load_suite, write_html

DEFAULT_SUITE = Path(__file__).resolve().parent / 'fixtures/ocr-quality/suite.json'
PIN_FIELDS = ('deployment_id', 'model_version_id', 'runtime_image_id', 'runtime_container_id', 'served_model_name')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def persist(path, report):
    save(path, report)
    write_html(report, path.with_suffix('.html'))


def child_path(path, item):
    return path.parent / 'refinement' / (item['id'] + '.json')


def canonical_path(path, item):
    return path.parent / 'canonical' / (item['id'] + '.json')


def initialize(api, model, deployment_id, suite_path, output_root):
    suite_path = suite_path.resolve()
    suite, checksum = load_suite(suite_path)  # Validate every PDF before any POST.
    key = str(uuid.uuid4())
    report = {'schema_version': 1, 'kind': 'ocr-quality', 'rubric_version': suite['rubric_version'],
              'key': key, 'api': api.base, 'model_api': model.base,
              'deployment_id': str(uuid.UUID(deployment_id)), 'suite_path': str(suite_path),
              'suite_sha256': checksum, 'suite': suite, 'suite_snapshot_sha256': digest(suite),
              'cases': [], 'passed': False}
    path = output_root / key / 'report.json'
    uploads = upload_report(api, [suite_path.parent / c['file'] for c in suite['cases']])['uploads']
    for case, upload in zip(suite['cases'], uploads):
        if upload['sha256'] != case['sha256']:
            raise ValueError('Fixture changed during preparation')
        subkey = str(uuid.uuid4())
        seed = {'key': subkey, 'name': 'ocr-quality-' + key + '-' + case['id'], 'upload': upload}
        item = {'id': case['id'], 'seed': seed, 'state': 'pending'}
        report['cases'].append(item)
        child = {'schema_version': 1, 'kind': 'refinement-smoke', 'api': api.base,
                 'idempotency_key': subkey, 'ocr_deployment_id': report['deployment_id'],
                 'items': [seed.copy()], 'passed': False}
        save(child_path(path, item), child)
    persist(path, report)  # All upload/refinement keys exist durably before first POST.
    return report, path


def validate_report(report, path, online):
    if (report.get('schema_version') != 1 or report.get('kind') != 'ocr-quality'
            or report.get('rubric_version') not in SUPPORTED_RUBRICS
            or report.get('rubric_version') != report['suite'].get('rubric_version')
            or report.get('suite_snapshot_sha256') != digest(report['suite'])):
        raise ValueError('Unsupported or changed quality report/rubric')
    cases = report['suite']['cases']
    ids = [c['id'] for c in cases]
    if not ids or len(ids) != len(set(ids)) or any(not re.fullmatch('[a-z0-9-]{1,60}', i) for i in ids):
        raise ValueError('Invalid recorded case IDs')
    if [i['id'] for i in report['cases']] != ids:
        raise ValueError('Recorded cases changed')
    if online:
        suite, checksum = load_suite(Path(report['suite_path']))
        if checksum != report['suite_sha256'] or suite != report['suite']:
            raise ValueError('Suite changed; original requests were not replayed')
    children = []
    for expected, item in zip(cases, report['cases']):
        child = json.loads(child_path(path, item).read_text(encoding='utf-8'))
        if (child.get('kind') != 'refinement-smoke' or child.get('schema_version') != 1
                or child.get('api') != report['api'] or child.get('ocr_deployment_id') != report['deployment_id']
                or child.get('idempotency_key') != item['seed']['key'] or len(child['items']) != 1
                or any(child['items'][0].get(k) != v for k, v in item['seed'].items())
                or item['seed']['upload']['sha256'] != expected['sha256']):
            raise ValueError('Refinement request identity changed: ' + item['id'])
        if online:
            checked_source(item['seed']['upload'])
        children.append(child)
    return children


def ready_deployment(model, report, path):
    code, dep = model.call('/api/deployments/' + report['deployment_id'])
    expect(code, 200, dep)
    if (dep.get('deployment_id') != report['deployment_id'] or dep.get('status') != 'ready'
            or dep.get('runtime_state') != 'ready' or dep.get('desired_state') != 'running'
            or dep.get('serving', {}).get('profile') != 'surya-ocr-v2' or dep.get('artifact_kind') != 'snapshot'
            or not all(dep.get(k) for k in PIN_FIELDS)):
        raise ValueError('Existing Surya snapshot deployment is not ready; no new deployment requested')
    pinned = {k: dep[k] for k in PIN_FIELDS}
    if report.get('pinned_runtime') and pinned != report['pinned_runtime']:
        raise ValueError('Surya runtime changed; this report cannot mix model/runtime identities')
    report['pinned_runtime'] = pinned
    persist(path, report)


def verified_document(report, child, content):
    if not child.get('passed') or not child['items'][0].get('passed'):
        raise ValueError('Canonical has no completed pipeline verification')
    item = child['items'][0]
    document = verify_result(item['source'], item['last_run'], item['canonical'], content)
    identity = item['last_run'].get('ocr') or {}
    if not report.get('pinned_runtime') or any(identity.get(k) != report['pinned_runtime'][k] for k in PIN_FIELDS):
        raise ValueError('Canonical OCR identity differs from the pinned deployment')
    if item['source']['sha256'] != item['upload']['sha256'] or item['source']['size_bytes'] != item['upload']['size_bytes']:
        raise ValueError('Canonical source differs from the selected fixture')
    return document, identity


def evaluate_cached(report, path, children):
    """Verify all completed bytes before allowing any further online mutation."""
    for case, item, child in zip(report['suite']['cases'], report['cases'], children):
        if item['state'] == 'evaluated':
            document, identity = verified_document(report, child, canonical_path(path, item).read_bytes())
            item['evaluation'] = evaluate_case(case, document, identity)
    finish(report, path)


def finish(report, path):
    report['passed'] = bool(report['cases']) and all(i['state'] == 'evaluated' and i.get('evaluation', {}).get('passed') for i in report['cases'])
    report['evaluated_cases'] = sum(i['state'] == 'evaluated' for i in report['cases'])
    report['passed_cases'] = sum(i.get('evaluation', {}).get('passed', False) for i in report['cases'])
    report['guard_passed_cases'] = sum(i.get('evaluation', {}).get('passed', False) and
                                       i['evaluation'].get('outcome') == 'coordinate_guard' for i in report['cases'])
    report['ocr_passed_cases'] = report['passed_cases'] - report['guard_passed_cases']
    persist(path, report)


def execute(report, path, timeout, offline=False):
    report['passed'] = False
    persist(path, report)
    children = validate_report(report, path, online=not offline)
    evaluate_cached(report, path, children)
    if offline:
        return
    data, model = API(report['api']), API(report['model_api'])
    for case, item, child in zip(report['suite']['cases'], report['cases'], children):
        if item['state'] in ('evaluated', 'execution_failed'):
            continue
        try:
            ready_deployment(model, report, path)
            last = child['items'][0].get('last_run', {})
            if last.get('status') in ('failed', 'cancelled'):
                raise ValueError('Previously recorded terminal refinement failure')
            if not child.get('passed'):
                refine(data, child, child_path(path, item), timeout)
            metadata = child['items'][0]['canonical']
            code, content, _ = data.call('/api/canonical-documents/' + metadata['document_id'] + '/content', binary=True, timeout=90)
            expect(code, 200, 'canonical quality content')
            document, identity = verified_document(report, child, content)
            target = canonical_path(path, item)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix('.tmp')
            temporary.write_bytes(content)
            temporary.replace(target)
            item['evaluation'] = evaluate_case(case, document, identity)
            item['state'] = 'evaluated'
            item.pop('error', None)
            success = 'GUARD_PASS' if item['evaluation'].get('outcome') == 'coordinate_guard' else 'PASS'
            print('[quality]', item['id'], success if item['evaluation']['passed'] else 'FAIL',
                  json.dumps([{'page': p['number'], 'cer': p['text']['cer'], 'order': p['reading_order']['passed'],
                               'geometry': p['geometry']['passed'], 'table': p['table']['passed']}
                              for p in item['evaluation']['pages']]), flush=True)
        except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
            item['error'] = str(exc)
            last = child['items'][0].get('last_run', {})
            if last.get('status') in ('failed', 'cancelled'):
                item['state'] = 'execution_failed'
                print('[quality]', item['id'], 'EXECUTION_FAILED', str(exc), flush=True)
            else:
                finish(report, path)
                raise  # Lost replies, timeouts, blocked submissions: reuse the same report.
        finish(report, path)  # A quality failure does not prevent measuring other cases.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api', default='http://127.0.0.1:14000')
    parser.add_argument('--model-api', default='http://127.0.0.1:13000')
    parser.add_argument('--deployment-id')
    parser.add_argument('--suite', type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--resume', type=Path)
    group.add_argument('--evaluate', type=Path)
    parser.add_argument('--timeout', type=int, default=1500, help='seconds per refinement run')
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error('--timeout must be positive')
    if args.resume or args.evaluate:
        if args.suite or args.deployment_id:
            parser.error('Resume/evaluate uses the recorded suite and deployment')
        path = (args.resume or args.evaluate).resolve()
        report = json.loads(path.read_text(encoding='utf-8'))
    else:
        if not args.deployment_id:
            parser.error('--deployment-id is required; use the existing ready Surya deployment')
        report, path = initialize(API(args.api), API(args.model_api), args.deployment_id,
                                  args.suite or DEFAULT_SUITE, Path(__file__).resolve().parent / 'results/ocr-quality')
    print('[record]', path, flush=True)
    print('[html]', path.with_suffix('.html'), flush=True)
    try:
        execute(report, path, args.timeout, offline=bool(args.evaluate))
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit('STOP: ' + str(exc) + '\nResume the same report: python tests/live_ocr_quality.py --resume ' + str(path))
    print('[quality-summary]', json.dumps({k: report[k] for k in ('passed', 'evaluated_cases', 'passed_cases', 'ocr_passed_cases', 'guard_passed_cases')}), flush=True)
    if not report['passed']:
        raise SystemExit('FAIL: quality checks failed or cases remain unmeasured; see report.html and report.json')
    if report['rubric_version'] == GEOMETRY_RUBRIC:
        print('PASS: geometry suite expectations met; guarded cases are NOT OCR successes. See separate OCR/guard counts.')
    else:
        print('PASS: Korean text, table values/spans, reading order, regions and selective OCR pages.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit('FAIL: ' + str(exc))
