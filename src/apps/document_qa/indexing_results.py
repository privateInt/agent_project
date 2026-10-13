"""Q05a immutable canonical snapshot and Knowledge readback gates; no I/O."""
from .contracts import (CanonicalRef, ContractError, Receipt, digest, document_snapshot,
                        integer, observe_index, uid)
from .intake import check
from .canonical_results import FORMATS, PROCESSOR, RULES


def prefixed_digest(value, prefix):
    return isinstance(value, str) and value.startswith(prefix) and digest(value[len(prefix):])


def defaults_checked(defaults):
    check(isinstance(defaults, dict) and {'embedding', 'chunker'} <= defaults.keys(),
          'INDEXING_DEFAULTS_NOT_READY', 409)
    profile, chunker = defaults['embedding'], defaults['chunker']
    fields = {'deployment_id', 'model_version_id', 'embedding_space_id', 'profile_id',
              'max_input_tokens', 'max_input_bytes'}
    # Q02 persists these display fields beside the immutable Model selection.
    # Project only known metadata away; unknown fields still fail closed.
    display = {'name', 'version', 'label'}
    check(isinstance(profile, dict) and fields <= set(profile) <= fields | display
          and all(isinstance(profile[key], str) for key in set(profile) & display),
          'INDEXING_PROFILE_INVALID', 409)
    profile = {key: profile[key] for key in fields}
    check(uid(profile['deployment_id']) and uid(profile['model_version_id'])
          and prefixed_digest(profile['embedding_space_id'], 'es_') and profile['profile_id'] == 'snowflake-ko-v1'
          and integer(profile['max_input_tokens'], 128, 8192)
          and integer(profile['max_input_bytes'], 1, 131072), 'INDEXING_PROFILE_INVALID', 409)
    check(isinstance(chunker, dict) and set(chunker) == {'id', 'version', 'options'}
          and chunker['id'] == 'structure_v1' and isinstance(chunker['version'], str)
          and 0 < len(chunker['version']) <= 100 and isinstance(chunker['options'], dict)
          and set(chunker['options']) == {'max_characters', 'overlap_characters'},
          'INDEXING_CHUNKER_INVALID', 409)
    size, overlap = chunker['options']['max_characters'], chunker['options']['overlap_characters']
    check(integer(size, 100, 8000) and integer(overlap, 0, 3999) and 2 * overlap < size
          and 4 * size <= profile['max_input_bytes'], 'INDEXING_CHUNKER_LIMIT_INVALID', 409)
    return {'embedding': dict(profile), 'chunker': {**chunker, 'options': dict(chunker['options'])}}


def canonical_input(job, refined):
    meta, receipt = refined.get('canonical'), refined.get('receipt')
    check(job['state'] == 'preparation_pending' and uid(job.get('source_document_id'))
          and refined.get('state') == 'canonical_verified' and refined.get('refinement_ready') is True
          and refined.get('terminal') is True and not refined.get('error_code')
          and isinstance(meta, dict) and isinstance(receipt, dict), 'INDEXING_CANONICAL_REQUIRED', 409)
    check(meta.get('state') == 'ready' and type(meta.get('issue_count')) is int and meta['issue_count'] == 0
          and meta.get('requires_ocr') is False and uid(meta.get('document_id'))
          and meta.get('source_document_id') == job['source_document_id']
          and uid(receipt.get('resource_id')) and uid(receipt.get('operation_id'))
          and meta.get('refinement_run_id') == receipt['resource_id']
          and meta.get('schema_version') == 'canonical-document-v1'
          and meta.get('processor_version') == PROCESSOR and meta.get('rules_version') == RULES
          and prefixed_digest(meta.get('processor_image_id'), 'sha256:'), 'INDEXING_CANONICAL_INVALID', 409)
    item = {'canonical_id': meta['document_id'], 'canonical_sha256': meta['sha256'],
            'size_bytes': meta['size_bytes'], 'source_document_id': job['source_document_id'],
            'source_sha256': job['sha256'], 'refinement_run_id': receipt['resource_id'],
            'schema_version': meta['schema_version'], 'processor_version': meta['processor_version'],
            'rules_version': meta['rules_version'], 'processor_image_id': meta['processor_image_id'],
            'format': FORMATS[job['filename'].rsplit('.', 1)[-1].lower()]}
    snapshot_checked([item])
    return item


def snapshot_checked(items):
    check(isinstance(items, list), 'INDEXING_SNAPSHOT_INVALID', 409)
    keys = {'canonical_id', 'canonical_sha256', 'size_bytes', 'source_document_id', 'source_sha256',
            'refinement_run_id', 'schema_version', 'processor_version', 'rules_version', 'processor_image_id', 'format'}
    check(all(isinstance(item, dict) and set(item) == keys and digest(item['source_sha256'])
              and uid(item['refinement_run_id']) and item['schema_version'] == 'canonical-document-v1'
              and item['processor_version'] == PROCESSOR and item['rules_version'] == RULES
              and prefixed_digest(item['processor_image_id'], 'sha256:') and item['format'] in FORMATS.values()
              for item in items), 'INDEXING_SNAPSHOT_INVALID', 409)
    refs = document_snapshot(tuple(CanonicalRef(item['canonical_id'], item['source_document_id'],
                                                item['canonical_sha256'], item['size_bytes']) for item in items))
    by_id = {item['canonical_id']: item for item in items}
    return [dict(by_id[ref.canonical_id]) for ref in refs]


def checked_index(body, index_id, profile, name):
    check(isinstance(body, dict) and body.get('index_id') == index_id and uid(index_id)
          and body.get('name') == name and body.get('embedding_deployment_id') == profile['deployment_id']
          and body.get('state') == 'active' and integer(body.get('revision'), 1)
          and (body.get('active_version_id') == '' or uid(body.get('active_version_id'))),
          'INDEXING_INDEX_MISMATCH', 502)
    return body


def checked_version(body, receipt, row, config):
    """A ready count alone cannot prove the exact input, chunker or Model binding."""
    items = snapshot_checked(row['documents'])
    refs = tuple(CanonicalRef(item['canonical_id'], item['source_document_id'],
                              item['canonical_sha256'], item['size_bytes']) for item in items)
    profile = config['embedding']
    result = observe_index(Receipt(**receipt['receipt']), body, row['index_id'], refs,
                           profile['deployment_id'], profile['embedding_space_id'])
    check(body.get('schema_version') == 'index-version-v1'
          and body.get('chunking_run_id') == receipt['chunking_run_id']
          and body.get('parent_version_id', '') == row['base_version_id'], 'INDEXING_VERSION_MISMATCH', 502)
    if result == 'failed':
        return result
    actual_profile = body.get('embedding_profile')
    check(isinstance(actual_profile, dict)
          and all(type(actual_profile.get(key)) is type(value) and actual_profile.get(key) == value
                  for key, value in profile.items()), 'INDEXING_PROFILE_CHANGED', 502)
    spec = body.get('input_spec')
    check(isinstance(spec, dict) and spec.get('schema_version') == 'chunking-spec-v1'
          and digest(spec.get('snapshot_id')) and isinstance(spec.get('documents'), list)
          and len(spec['documents']) == len(items), 'INDEXING_INPUT_SPEC_MISMATCH', 502)
    actual = spec['documents']
    actual = sorted(actual, key=lambda item: str(item.get('canonical_id')) if isinstance(item, dict) else '')
    check(all(isinstance(current, dict) and set(current) == set(expected)
              and all(type(current[key]) is type(value) and current[key] == value
                      for key, value in expected.items())
              for current, expected in zip(actual, items)),
          'INDEXING_INPUT_SPEC_MISMATCH', 502)
    selection = spec.get('chunker')
    check(isinstance(selection, dict) and digest(selection.get('policy_hash'))
          and selection.get('id') == config['chunker']['id']
          and selection.get('version') == config['chunker']['version']
          and isinstance(selection.get('options'), dict)
          and set(selection['options']) == set(config['chunker']['options'])
          and all(type(selection['options'][key]) is type(value) and selection['options'][key] == value
                  for key, value in config['chunker']['options'].items()),
          'INDEXING_CHUNKER_CHANGED', 502)
    if result == 'complete':
        check(digest(body.get('chunk_set_sha256')), 'INDEXING_CHUNK_SET_UNVERIFIED', 502)
    return result
