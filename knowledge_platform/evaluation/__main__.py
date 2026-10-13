"""Prepare, annotate, validate and evaluate existing Knowledge indexes."""
import argparse
import json
from pathlib import Path
import sys

from .common import (Client, PACKET_LIMIT, DATASET_LIMIT, REPORT_LIMIT, require,
                     read_json, save_json, encoded, identity)
from .dataset import prepare, validate_dataset, add_judgment
from .runner import run, compare, safe_error


def inputs(args):
    packet, _ = read_json(args.sources, PACKET_LIMIT)
    dataset, sha = read_json(args.dataset, DATASET_LIMIT)
    return packet, dataset, sha


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare', help='Read pinned canonical sources; no inference or new indexing')
    prep.add_argument('--index-id', required=True)
    prep.add_argument('--index-version-id', required=True)
    prep.add_argument('--reranker-deployment-id', default='')
    prep.add_argument('--knowledge-url', default='http://127.0.0.1:15100')
    prep.add_argument('--model-url', default='http://127.0.0.1:13000')
    prep.add_argument('--data-url', default='http://127.0.0.1:14000')
    prep.add_argument('--output-dir', type=Path, required=True)
    for name in ('add-case', 'review', 'validate', 'run'):
        child = sub.add_parser(name)
        child.add_argument('--sources', type=Path, required=True)
        child.add_argument('--dataset', type=Path, required=True)
        if name == 'add-case':
            for field in ('case-id', 'question', 'canonical-id', 'block-id', 'quote'):
                child.add_argument('--' + field, required=True)
            child.add_argument('--grade', type=int, default=3)
            child.add_argument('--start', type=int)
        if name == 'review':
            child.add_argument('--reviewer', required=True, help='Reviewer who checked every question and source anchor')
        if name == 'run':
            child.add_argument('--variants', type=Path, required=True)
            child.add_argument('--output', type=Path, required=True)
            child.add_argument('--knowledge-url', default='http://127.0.0.1:15100')
            child.add_argument('--model-url', default='http://127.0.0.1:13000')
    c = sub.add_parser('compare')
    c.add_argument('--left', type=Path, required=True)
    c.add_argument('--right', type=Path, required=True)
    c.add_argument('--left-variant', required=True)
    c.add_argument('--right-variant', required=True)
    c.add_argument('--output', type=Path, required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == 'prepare':
        out = args.output_dir.expanduser()
        require(not out.exists(), 'Output directory exists; use a new directory to preserve annotations')
        client = Client()
        packet, dataset, config = prepare(args.index_id, args.index_version_id, args.knowledge_url,
                                          args.data_url, args.model_url, args.reranker_deployment_id, client, client.raw)
        for item, cap in ((packet, PACKET_LIMIT), (dataset, DATASET_LIMIT), (config, DATASET_LIMIT)):
            require(len(encoded(item)) <= cap, 'Prepared file exceeds limit')
        out.mkdir(mode=0o700)
        for name, value, cap in (('sources.json', packet, PACKET_LIMIT), ('dataset.json', dataset, DATASET_LIMIT), ('variants.json', config, DATASET_LIMIT)):
            save_json(out / name, value, cap)
        print(json.dumps({'status': 'prepared', 'documents': len(packet['documents']), 'blocks': sum(len(d['blocks']) for d in packet['documents']),
                          'snapshot_id': packet['snapshot_id'], 'questions': 0, 'review': 'draft', 'model_inference_requests': 0, 'lifecycle_changes': 0,
                          'next': 'Review sources.json and register actual questions/evidence before evaluation'}, ensure_ascii=False, indent=2))
        return 0
    if args.command == 'compare':
        require(not args.output.exists(), 'Output exists; select a new path')
        left, _ = read_json(args.left, REPORT_LIMIT); right, _ = read_json(args.right, REPORT_LIMIT)
        result = compare(left, right, args.left_variant, args.right_variant)
        save_json(args.output, result, REPORT_LIMIT)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    packet, dataset, dataset_hash = inputs(args)
    if args.command == 'add-case':
        dataset = add_judgment(dataset, packet, args.case_id, args.question, args.canonical_id, args.block_id, args.quote, args.grade, args.start)
        save_json(args.dataset, dataset, DATASET_LIMIT)
        print('Saved evidence. Dataset review status is now draft.')
        return 0
    if args.command == 'review':
        dataset['review'] = {'status': 'reviewed', 'reviewer': args.reviewer}
        validate_dataset(dataset, packet)
        save_json(args.dataset, dataset, DATASET_LIMIT)
        print('Dataset validated and marked reviewed; semantic correctness is the reviewer\'s judgment.')
        return 0
    validate_dataset(dataset, packet)
    if args.command == 'validate':
        print(json.dumps({'status': 'valid', 'questions': len(dataset['cases']), 'dataset_sha256': dataset_hash, 'sources_sha256': identity(packet)}, indent=2))
        return 0
    config, _ = read_json(args.variants, DATASET_LIMIT)
    require(not args.output.exists(), 'Output exists; select a new path')
    # Check the destination before spending inference. The first checkpoint owns
    # this new report; later checkpoints replace only this explicit output.
    require(args.output.parent.is_dir(), 'Output parent directory does not exist')
    def checkpoint(report):
        save_json(args.output, report, REPORT_LIMIT)
    try:
        report = run(dataset, packet, config, dataset_hash, args.knowledge_url, args.model_url, Client(), checkpoint)
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
        if not args.output.exists():
            checkpoint({'schema_version': 'retrieval-report-v1', 'status': 'preflight_failed',
                        'dataset_sha256': dataset_hash, 'error': safe_error(error, 'preflight'), 'summary': None})
        raise
    print(json.dumps({'status': report['status'], 'query_embedding_requests': report['query_embedding_requests'],
                      'search_requests': report['search_requests'], 'lifecycle_changes': 0, 'summary': report['summary']}, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'completed' else 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
        message = str(error) if isinstance(error, ValueError) else 'Input, response or output does not match the contract'
        raise SystemExit('FAIL: ' + message) from None
