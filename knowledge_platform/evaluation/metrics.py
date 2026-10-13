"""Deterministic source-span matching; no network or model dependencies."""
import math

NAMES = ('hit_rate', 'recall', 'mrr', 'ndcg')


def coverage(hit, judgment):
    if (hit['canonical_id'], hit['source_document_id']) != (judgment['canonical_id'], judgment['source_document_id']):
        return 0.0
    intervals = sorted((max(s['start'], judgment['start']), min(s['end'], judgment['end']))
                       for s in hit['source_spans'] if s['block_id'] == judgment['block_id']
                       and max(s['start'], judgment['start']) < min(s['end'], judgment['end']))
    covered, end = 0, -1
    for a, b in intervals:
        covered += max(0, b - max(a, end))
        end = max(end, b)
    return covered / (judgment['end'] - judgment['start'])


def assign(hits, judgments, threshold):
    remaining = {j['judgment_id']: j for j in judgments}
    assignments = []
    for hit in hits:
        possible = [j for j in remaining.values() if coverage(hit, j) >= threshold]
        chosen = min(possible, key=lambda j: (-j['grade'], j['judgment_id'])) if possible else None
        assignments.append({'chunk_id': hit['chunk_id'], 'judgment_id': chosen['judgment_id'] if chosen else None,
                            'grade': chosen['grade'] if chosen else 0})
        if chosen:
            del remaining[chosen['judgment_id']]
    return assignments


def zero():
    return {name: 0.0 for name in NAMES}


def dcg(grades):
    return math.fsum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(grades))


def metrics(assignments, judgments, k):
    grades = [a['grade'] for a in assignments[:k]]
    first = next((i for i, g in enumerate(grades, 1) if g), None)
    ideal = dcg(sorted((j['grade'] for j in judgments), reverse=True)[:k])
    return {'hit_rate': float(first is not None), 'recall': sum(g > 0 for g in grades) / len(judgments),
            'mrr': 1 / first if first else 0.0, 'ndcg': dcg(grades) / ideal if ideal else 0.0}


def mean(rows):
    return {name: math.fsum(r[name] for r in rows) / len(rows) if rows else 0.0 for name in NAMES}


def summarize(cases, variant_ids, planned):
    output = {}
    for vid in variant_ids:
        entries = [c['variants'][vid] for c in cases]
        good = [e for e in entries if e['status'] == 'ok']
        failed = len(entries) - len(good)
        # Unfinished reports expose a planned denominator and not-run count.
        all_metrics = [e['metrics'] if e['status'] == 'ok' else zero() for e in entries]
        all_metrics += [zero() for _ in range(planned - len(entries))]
        output[vid] = {
            'planned': planned, 'succeeded': len(good), 'failed': failed, 'not_run': planned - len(entries),
            'failure_rate': failed / planned, 'success_rate': len(good) / planned,
            'all_questions_metrics': mean(all_metrics), 'successful_only_metrics': mean([e['metrics'] for e in good]) if good else None,
            'candidate_observed': sum('candidate_recall' in e for e in entries),
            'candidate_recall_all_questions': math.fsum(e.get('candidate_recall', 0) for e in entries) / planned,
            'mean_successful_search_wall_ms': math.fsum(e['search_wall_ms'] for e in good) / len(good) if good else None,
        }
    return output
