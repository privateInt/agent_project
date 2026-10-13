"""Pinned, disjoint chat data and explicit final-assistant evaluation tokens."""
import hashlib
import json
from pathlib import Path
import unicodedata

PROTOCOL = 'final_assistant_nll_v1'
MAX_RECORDS = 2048


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def read_pinned(path, pin):
    raw = Path(path).read_bytes()
    if pin.get('source_type') != 'version' or len(raw) != pin['size_bytes'] or sha(raw) != pin['sha256']:
        raise ValueError('evaluation input does not match its registered dataset version')
    records = [json.loads(line, object_pairs_hook=unique_object) for line in raw.decode('utf-8').splitlines() if line.strip()]
    if len(records) != pin['record_count'] or len(records) < 2:
        raise ValueError('evaluation input record count changed')
    for record in records:
        messages(record)
    return records


def messages(record):
    if not isinstance(record, dict) or set(record) != {'messages'}:
        raise ValueError('evaluation requires messages-only JSONL')
    values = record['messages']
    if not isinstance(values, list) or not 2 <= len(values) <= 256:
        raise ValueError('evaluation requires 2..256 messages per record')
    expected = 'user'
    for i, item in enumerate(values):
        if (not isinstance(item, dict) or set(item) != {'role', 'content'} or
                not isinstance(item['content'], str) or not item['content'].strip()):
            raise ValueError('invalid evaluation message')
        if i == 0 and item['role'] == 'system':
            continue
        if item['role'] != expected:
            raise ValueError('evaluation requires alternating user/assistant turns and optional initial system')
        expected = 'assistant' if expected == 'user' else 'user'
    if values[-1]['role'] != 'assistant':
        raise ValueError('evaluation requires a final assistant reference answer')
    return values


def prompt_hashes(record):
    digest = hashlib.sha256()
    result = []
    for item in messages(record):
        if item['role'] == 'assistant':
            result.append(digest.hexdigest())
        normalized = [item['role'], unicodedata.normalize('NFC', item['content']).strip()]
        raw = canonical(normalized)
        digest.update(len(raw).to_bytes(8, 'big'))
        digest.update(raw)
    return result


def check_disjoint(train, evaluation):
    training_prompts = {h for r in train for h in prompt_hashes(r)}
    seen = set()
    for record in evaluation:
        # Check every evaluation turn, including earlier references in its context.
        hashes = prompt_hashes(record)
        if training_prompts.intersection(hashes):
            raise ValueError('training/evaluation conversation prompts overlap')
        if hashes[-1] in seen:
            raise ValueError('duplicate final evaluation prompt')
        seen.add(hashes[-1])


def token_ids(tokenizer, conversation, generation_prompt):
    ids = tokenizer.apply_chat_template(conversation, tokenize=True,
                                       add_generation_prompt=generation_prompt, return_dict=False)
    if not isinstance(ids, list) or not ids or any(type(i) is not int or i < 0 for i in ids):
        raise ValueError('chat template did not produce a nonempty token ID list')
    return ids


def encode_evaluation(tokenizer, train, evaluation, limit):
    training_prompts = set()
    for record in train:
        conversation = messages(record)
        for i, item in enumerate(conversation):
            if item['role'] == 'assistant':
                training_prompts.add(sha(canonical(token_ids(tokenizer, conversation[:i], True))))
    encoded, seen = [], set()
    for record in evaluation:
        conversation = messages(record)
        for i, item in enumerate(conversation):
            if item['role'] == 'assistant' and sha(canonical(token_ids(tokenizer, conversation[:i], True))) in training_prompts:
                raise ValueError('training/evaluation prompts overlap after tokenization')
        prefix = token_ids(tokenizer, conversation[:-1], True)
        full = token_ids(tokenizer, conversation, False)
        if len(full) > limit:
            raise ValueError('evaluation sample exceeds sequence_length; truncation is not permitted')
        if len(prefix) >= len(full) or full[:len(prefix)] != prefix:
            raise ValueError('chat template cannot isolate the final assistant answer as a token suffix')
        prompt_hash = sha(canonical(prefix))
        if prompt_hash in seen:
            raise ValueError('duplicate evaluation prompt after tokenization')
        seen.add(prompt_hash)
        encoded.append({'input_ids': full, 'prefix_tokens': len(prefix)})
    return encoded


def prepare(spec, workspace):
    from checkpoint_store import manifest
    request = spec['evaluation']
    pin = request['spec']['dataset']
    if not 2 <= pin['record_count'] <= MAX_RECORDS:
        raise ValueError('evaluation requires 2..2048 records')
    train = read_pinned(spec['dataset']['local_path'], request['train_dataset'])
    evaluation = read_pinned(request['dataset_path'], pin)
    check_disjoint(train, evaluation)
    training = spec['training']
    if spec['world_size'] != 1 or training['sample_packing'] or not 2 <= training['sequence_length'] <= 4096:
        raise ValueError('evaluation requires single GPU, unpacked data and sequence_length 2..4096')
    context = dict(request, training_path=spec['dataset']['local_path'], sequence_length=training['sequence_length'],
                   max_steps=training['max_steps'], base_sha256=sha(canonical(manifest(Path(spec['model_dir'])))),
                   tracking_enabled=bool(spec.get('tracking', {}).get('enabled')))
    (workspace / 'evaluation-context.json').write_bytes(canonical(context))
    return context
