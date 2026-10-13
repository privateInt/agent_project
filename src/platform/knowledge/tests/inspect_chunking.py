#!/usr/bin/env python3
"""Read existing chunk runs and b90 provenance. Creates no runs and performs no OCR."""
import argparse
from collections import Counter
import json
import uuid
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:15100')
    parser.add_argument('--run-id', action='append')
    parser.add_argument('--canonical-id', default='157c335f-5436-4338-8078-edb040ebe1ca')
    parser.add_argument('--block-id', default='b90')
    args = parser.parse_args()
    canonical = str(uuid.UUID(args.canonical_id))
    runs = args.run_id or ['6e3eaf24-c2dc-4756-87bb-83468a96c827']
    if len(runs) > 10:
        raise ValueError('At most 10 existing runs per inspection')
    base = args.base_url.rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('base-url must be an HTTP(S) origin')
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def get(path):
        with opener.open(Request(base + path, method='GET'), timeout=45) as response:
            raw = response.read((16 << 20)+1)
            if len(raw) > 16 << 20:
                raise ValueError('Response exceeded 16 MiB inspection limit')
            return json.loads(raw)

    output = []
    for run_id in runs:
        run_id = str(uuid.UUID(run_id))
        path = '/api/chunking-runs/' + run_id
        run = get(path)
        chunks, cursor, seen = [], '', set()
        while True:
            page = get(path + '/chunks?' + urlencode({'limit': 100, **({'cursor': cursor} if cursor else {})}))
            chunks.extend(page.get('items') or [])
            cursor = page.get('next_cursor') or ''
            if len(chunks) > 20000 or (cursor and cursor in seen):
                raise ValueError('Invalid chunk pagination')
            if not cursor:
                break
            seen.add(cursor)
        if len(chunks) != run.get('chunk_count'):
            raise ValueError('Stored count differs from paginated results')
        target = []
        for chunk in chunks:
            spans = [s for s in chunk.get('source_spans', []) if s.get('block_id') == args.block_id]
            if chunk.get('canonical_id') == canonical and spans:
                target.append({'ordinal': chunk.get('ordinal'), 'kind': chunk.get('kind'), 'characters': len(chunk.get('text') or ''),
                               'source_spans': [{k: s.get(k) for k in ('block_id', 'start', 'end', 'chunk_start', 'chunk_end')} for s in spans],
                               'structure': chunk.get('structure'), 'quality_state': (chunk.get('quality') or {}).get('state'),
                               'text_preview': (chunk.get('text') or '')[:2000]})
        output.append({'chunking_run_id': run_id, 'spec': run.get('spec'), 'summary': run.get('summary'),
                       'chunk_count': len(chunks), 'kind_counts': dict(Counter(c.get('kind') for c in chunks)),
                       'maximum_characters': max((len(c.get('text') or '') for c in chunks), default=0),
                       'target_chunks': target, 'target_count': len(target), 'same_snapshot_comparison_requires_matching_spec_snapshot_id': True})
    raw = json.dumps({'scope': 'read-only existing results; source quality is unchanged', 'runs': output}, ensure_ascii=False, indent=2)
    if len(raw.encode()) > 128 << 10:
        raise ValueError('Inspection summary exceeds 128 KiB; inspect fewer runs')
    print(raw)


if __name__ == '__main__':
    main()
