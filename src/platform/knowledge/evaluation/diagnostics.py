"""Bounded evidence for exact candidate mismatches; no source text or vectors."""
from .common import encoded, identity


IDENTITY_FIELDS = (
    'text', 'content_sha256', 'source_document_id', 'canonical_id', 'kind',
    'source_spans', 'quality', 'structure', 'retrieval_rank', 'retrieval_score',
)
DETAIL_LIMIT = 4 << 10
REPORT_DETAIL_LIMIT = 48 << 10


class CandidateMismatch(ValueError):
    def __init__(self, code, detail):
        super().__init__(code)
        self.detail = detail


def candidate_fingerprint(rows):
    """Hash vector order, not the final reranked order; matches old OFF hashes."""
    ordered = sorted(rows, key=lambda h: (h['retrieval_rank'], h['chunk_id']))
    return identity([(h['chunk_id'], h['retrieval_rank'], h['retrieval_score']) for h in ordered])


def candidate_equal(off, on):
    """Preserve the original exact comparison and error-code precedence.

    Call after API validation. Rank and score receipts are finite and bounded;
    arbitrary text/metadata is represented only by normalized JSON hashes.
    """
    before = {h['chunk_id']: h for h in off}
    after = {h['chunk_id']: h for h in on}
    same_set = len(off) == len(on) and before.keys() == after.keys()
    changed = []
    changed_fields = set()
    for cid in sorted(before.keys() & after.keys()):
        a, b = before[cid], after[cid]
        names = [k for k in IDENTITY_FIELDS if a.get(k) != b.get(k)]
        if not names:
            continue
        changed_fields.update(names)
        values = {}
        for name in names:
            left, right = a.get(name), b.get(name)
            if name in ('retrieval_rank', 'retrieval_score'):
                values[name] = {'off': left, 'on': right}
                if name == 'retrieval_score':
                    values[name]['on_minus_off'] = right - left
            else:
                values[name] = {'off_sha256': identity(left), 'on_sha256': identity(right)}
        changed.append({'chunk_id': cid, 'fields': values})
    if same_set and not changed:
        return
    only_off, only_on = sorted(before.keys() - after.keys()), sorted(after.keys() - before.keys())
    detail = {
        'schema_version': 'candidate-difference-v1',
        'off_count': len(off), 'on_count': len(on), 'same_candidate_ids': same_set,
        'off_candidates_sha256': candidate_fingerprint(off),
        'on_candidates_sha256': candidate_fingerprint(on),
        'only_off_count': len(only_off), 'only_on_count': len(only_on),
        'only_off': only_off[:5], 'only_on': only_on[:5],
        'changed_count': len(changed), 'changed_fields': sorted(changed_fields),
        'changes': changed[:3],
    }
    # Counts cover all candidates; bounded samples never change failure status.
    while True:
        detail['omitted_only_off_count'] = len(only_off) - len(detail['only_off'])
        detail['omitted_only_on_count'] = len(only_on) - len(detail['only_on'])
        detail['omitted_changed_count'] = len(changed) - len(detail['changes'])
        detail['truncated'] = any(detail[k] for k in (
            'omitted_only_off_count', 'omitted_only_on_count', 'omitted_changed_count'))
        if len(encoded(detail)) <= DETAIL_LIMIT:
            break
        if len(detail['changes']) > 1:
            detail['changes'].pop()
        elif detail['only_off']:
            detail['only_off'].pop()
        elif detail['only_on']:
            detail['only_on'].pop()
        else:
            # One unusually large diagnostic must not prevent failure reporting.
            detail['changes'].pop()
    code = 'CANDIDATE_IDENTITY_CHANGED' if same_set else 'CANDIDATE_SET_CHANGED'
    raise CandidateMismatch(code, detail)
