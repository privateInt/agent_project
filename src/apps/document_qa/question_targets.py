"""Q06b-1 bounded immutable question target references. No I/O or readiness proof."""
import json
import re
from uuid import uuid5

from .contracts import (CanonicalRef, ContractError, NAMESPACE, QuestionHead,
                        digest, fingerprint, integer, strict_json, uid, validate_head)
from .indexing_results import snapshot_checked
from .intake import check

MAX_TARGET_BYTES = 128 << 10
MAX_REVISION = (1 << 63) - 1
FIELDS = {'schema', 'library_id', 'revision', 'indexing_generation', 'documents',
          'index_id', 'index_version_id', 'embedding_deployment_id',
          'embedding_space_id', 'tool_version_id', 'agent_version_id', 'pins'}
UUID_PINS = {'embedding_model_version_id', 'knowledge_connection_version_id',
             'embedding_connection_version_id', 'chat_connection_version_id',
             'chat_deployment_id', 'chat_model_version_id'}
HASH_PINS = {'tool_definition_sha256', 'agent_definition_sha256'}
PROFILE_PINS = {'embedding_profile_id', 'chat_profile_id'}


def encoded_target(body):
    try:
        raw = json.dumps(body, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ContractError('QUESTION_TARGET_INVALID') from None
    check(0 < len(raw) <= MAX_TARGET_BYTES, 'QUESTION_TARGET_TOO_LARGE', 413)
    return raw


def as_head(body):
    return QuestionHead(body['library_id'], body['revision'],
                        tuple(CanonicalRef(d['canonical_id'], d['source_document_id'],
                                           d['canonical_sha256'], d['size_bytes'])
                              for d in body['documents']),
                        *(body[k] for k in ('index_id', 'index_version_id',
                                          'embedding_deployment_id', 'embedding_space_id',
                                          'tool_version_id', 'agent_version_id')))


def checked_target(body):
    check(isinstance(body, dict) and set(body) == FIELDS
          and body['schema'] == 'question-target-v1'
          and integer(body['revision'], 1, MAX_REVISION)
          and integer(body['indexing_generation'], 1, MAX_REVISION),
          'QUESTION_TARGET_INVALID', 409)
    encoded_target(body)  # Bound untrusted strings before copying/canonicalizing.
    pins = body['pins']
    check(isinstance(pins, dict) and set(pins) == UUID_PINS | HASH_PINS | PROFILE_PINS
          and all(uid(pins[k]) for k in UUID_PINS)
          and all(digest(pins[k]) for k in HASH_PINS)
          and all(isinstance(pins[k], str) and re.fullmatch(r'[a-z][a-z0-9._-]{0,99}', pins[k])
                  for k in PROFILE_PINS), 'QUESTION_TARGET_PINS_INVALID', 409)
    normalized = {**body, 'documents': snapshot_checked(body['documents']), 'pins': dict(pins)}
    validate_head(as_head(normalized))
    return normalized


def decoded_target(raw):
    check(isinstance(raw, bytes) and 0 < len(raw) <= MAX_TARGET_BYTES,
          'QUESTION_TARGET_TOO_LARGE', 413)
    return checked_target(strict_json(raw))


def target_id(body):
    return str(uuid5(NAMESPACE, 'q06b1:' + fingerprint(body)))
