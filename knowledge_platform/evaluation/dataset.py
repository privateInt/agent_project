"""Prepare source material and validate human-reviewed evidence ranges."""
import copy
from pathlib import Path

from .common import (PACKET_LIMIT, DATASET_LIMIT, PIN_FIELDS, require, fields, sha, label,
                     digest, identity, encoded, save_json, strict_json, pins, version_ready,
                     valid_uuid, valid_reranker)


def validate_packet(packet):
    fields(packet, ('schema_version', 'snapshot_id', 'documents'))
    require(packet['schema_version'] == 'retrieval-sources-v1' and sha(packet['snapshot_id']), 'Invalid source packet identity')
    require(isinstance(packet['documents'], list) and 1 <= len(packet['documents']) <= 20, 'Invalid source packet documents')
    seen = set()
    for doc in packet['documents']:
        fields(doc, (*PIN_FIELDS, 'quality', 'blocks'))
        require(valid_uuid(doc['canonical_id']) and valid_uuid(doc['source_document_id'])
                and sha(doc['canonical_sha256']) and sha(doc['source_sha256'])
                and doc['canonical_id'] not in seen, 'Invalid or duplicate source document')
        seen.add(doc['canonical_id'])
        require(isinstance(doc['quality'], dict) and isinstance(doc['blocks'], list), 'Source quality/blocks missing')
        blocks = set()
        for block in doc['blocks']:
            fields(block, ('id', 'type', 'text', 'text_sha256', 'location', 'recognition_status'))
            require(isinstance(block['id'], str) and label(block['id']) and block['id'] not in blocks
                    and isinstance(block['text'], str) and sha(block['text_sha256'])
                    and digest(block['text'].encode()) == block['text_sha256'], 'Invalid source block identity')
            blocks.add(block['id'])
    require(len(encoded(packet)) <= PACKET_LIMIT, 'Source packet exceeds 512 KiB')
    return packet


def packet_pins(packet):
    return sorted(({k: d[k] for k in PIN_FIELDS} for d in packet['documents']), key=lambda d: d['canonical_id'])


def validate_dataset(dataset, packet, reviewed=True):
    validate_packet(packet)
    fields(dataset, ('schema_version', 'dataset_id', 'snapshot_id', 'sources_sha256', 'documents', 'coverage_threshold', 'review', 'cases'))
    require(dataset['schema_version'] == 'retrieval-dataset-v1' and label(dataset['dataset_id']), 'Invalid dataset identity')
    require(dataset['snapshot_id'] == packet['snapshot_id'] and dataset['sources_sha256'] == identity(packet)
            and dataset['documents'] == packet_pins(packet), 'Dataset source packet identity differs')
    threshold = dataset['coverage_threshold']
    require(type(threshold) in (int, float) and .5 <= threshold <= 1, 'Coverage must be 0.5..1.0')
    fields(dataset['review'], ('status', 'reviewer'))
    require(dataset['review']['status'] in ('draft', 'reviewed') and isinstance(dataset['review']['reviewer'], str)
            and len(dataset['review']['reviewer']) <= 100, 'Invalid review state')
    if reviewed:
        require(dataset['review']['status'] == 'reviewed' and dataset['review']['reviewer'].strip(), 'Dataset requires human review')
    cases = dataset['cases']
    require(isinstance(cases, list) and (1 if reviewed else 0) <= len(cases) <= 100, 'Dataset requires 1..100 answerable cases')
    sources = {d['canonical_id']: d for d in packet['documents']}
    case_ids, questions = set(), set()
    for case in cases:
        fields(case, ('case_id', 'question', 'judgments'))
        require(label(case['case_id']) and case['case_id'] not in case_ids, 'Invalid or duplicate case ID')
        question = case['question']
        require(isinstance(question, str) and question.strip() and '\0' not in question
                and len(question.encode()) <= 8192 and question.strip() not in questions, 'Invalid or duplicate question')
        case_ids.add(case['case_id']); questions.add(question.strip())
        require(isinstance(case['judgments'], list) and 1 <= len(case['judgments']) <= 20, 'Each case requires 1..20 judgments')
        ids, anchors = set(), []
        for j in case['judgments']:
            fields(j, ('judgment_id', 'source_document_id', 'canonical_id', 'block_id', 'start', 'end', 'quote_sha256', 'grade'))
            require(label(j['judgment_id']) and j['judgment_id'] not in ids and type(j['grade']) is int and 1 <= j['grade'] <= 3, 'Invalid judgment ID or grade')
            ids.add(j['judgment_id'])
            doc = sources.get(j['canonical_id'])
            require(doc is not None and doc['source_document_id'] == j['source_document_id'], 'Judgment source differs')
            block = next((b for b in doc['blocks'] if b['id'] == j['block_id']), None)
            a, b = j['start'], j['end']
            require(block is not None and type(a) is int and type(b) is int and 0 <= a < b <= len(block['text']), 'Judgment range is invalid')
            quote = block['text'][a:b]
            require(quote.strip() and digest(quote.encode()) == j['quote_sha256'], 'Judgment quote hash differs')
            require(not any(cid == j['canonical_id'] and bid == j['block_id'] and max(a, x) < min(b, y) for cid, bid, x, y in anchors), 'Overlapping judgments would duplicate evidence')
            anchors.append((j['canonical_id'], j['block_id'], a, b))
    require(len(encoded(dataset)) <= DATASET_LIMIT, 'Dataset exceeds 256 KiB')
    return dataset


def prepare(index_id, version_id, knowledge_url, data_url, model_url, reranker, call, fetch_raw):
    require(valid_uuid(index_id) and valid_uuid(version_id) and (not reranker or valid_uuid(reranker)), 'Explicit UUIDs required')
    index = call(knowledge_url, '/api/indexes/' + index_id)
    require(index.get('index_id') == index_id and index.get('state') == 'active', 'Index unavailable')
    version = version_ready(call(knowledge_url, '/api/index-versions/' + version_id), index_id, version_id)
    selected = pins(version)
    manifests = {d['canonical_id']: d for d in version['input_spec']['documents']}
    documents = []
    total = 0
    for pin in selected:
        expected = manifests[pin['canonical_id']]
        require(type(expected.get('size_bytes')) is int and 1 <= expected['size_bytes'] <= 32 << 20, 'Canonical size invalid')
        total += expected['size_bytes']
        require(total <= 32 << 20, 'Combined canonical bytes exceed 32 MiB')
        raw = fetch_raw(data_url, '/api/canonical-documents/' + pin['canonical_id'] + '/content', limit=expected['size_bytes'])
        require(len(raw) == expected['size_bytes'] and digest(raw) == pin['canonical_sha256'], 'Canonical content hash/size differs from index')
        doc = strict_json(raw.decode('utf-8'))
        require(doc.get('schema_version') == 'canonical-document-v1' and doc.get('source', {}).get('document_id') == pin['source_document_id']
                and doc['source'].get('sha256') == pin['source_sha256'], 'Canonical source identity differs')
        blocks = []
        for b in doc['blocks']:
            text = b.get('text') or ''
            require(isinstance(text, str), 'Canonical text invalid')
            blocks.append({'id': b['id'], 'type': b['type'], 'text': text, 'text_sha256': digest(text.encode()),
                           'location': b.get('location', {}), 'recognition_status': b.get('recognition_status', '')})
        documents.append({**pin, 'quality': doc['quality'], 'blocks': blocks})
    packet = validate_packet({'schema_version': 'retrieval-sources-v1', 'snapshot_id': version['input_spec']['snapshot_id'], 'documents': documents})
    dataset = {'schema_version': 'retrieval-dataset-v1', 'dataset_id': 'research-v1', 'snapshot_id': packet['snapshot_id'],
               'sources_sha256': identity(packet), 'documents': selected, 'coverage_threshold': 1.0,
               'review': {'status': 'draft', 'reviewer': ''}, 'cases': []}
    base = {'id': 'baseline-off', 'index_id': index_id, 'index_version_id': version_id, 'embedding_deployment_id': version['embedding_profile']['deployment_id'], 'rerank': None}
    variants = {'schema_version': 'retrieval-variants-v1', 'candidate_k': 20, 'top_k': 5, 'variants': [base]}
    if reranker:
        profile = call(model_url, '/api/deployments/' + reranker + '/reranker-profile')
        require(valid_reranker(profile, reranker), 'Reranker profile unavailable')
        variants['variants'].append({**base, 'id': 'baseline-on', 'rerank': {'deployment_id': reranker, 'expected_reranker_profile_id': profile['reranker_profile_id']}})
    return packet, dataset, variants


def add_judgment(dataset, packet, case_id, question, canonical_id, block_id, quote, grade=3, start=None):
    validate_dataset(dataset, packet, reviewed=False)
    require(isinstance(quote, str) and quote.strip(), 'An exact nonempty source quote is required')
    doc = next((d for d in packet['documents'] if d['canonical_id'] == canonical_id), None)
    require(doc is not None, 'Unknown canonical ID')
    block = next((b for b in doc['blocks'] if b['id'] == block_id), None)
    require(block is not None, 'Unknown block ID')
    text = block['text']
    if start is None:
        start = text.find(quote)
        require(start >= 0 and text.find(quote, start + 1) == -1, 'Quote missing or ambiguous; specify its code point start')
    require(type(start) is int and start >= 0 and text[start:start + len(quote)] == quote, 'Quote and start do not match source')
    out = copy.deepcopy(dataset)
    case = next((c for c in out['cases'] if c['case_id'] == case_id), None)
    if case is None:
        case = {'case_id': case_id, 'question': question, 'judgments': []}; out['cases'].append(case)
    require(case['question'] == question, 'Existing case has a different question')
    case['judgments'].append({'judgment_id': 'e' + str(len(case['judgments']) + 1), 'source_document_id': doc['source_document_id'],
                              'canonical_id': canonical_id, 'block_id': block_id, 'start': start, 'end': start + len(quote),
                              'quote_sha256': digest(quote.encode()), 'grade': grade})
    out['review'] = {'status': 'draft', 'reviewer': ''}
    validate_dataset(out, packet, reviewed=False)
    return out
