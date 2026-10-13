"""Inspect a Deployment's cached base/adapter without changing model files.
Reads bounded tokenizer JSON and safetensors headers through docker exec.
Does not start containers, allocate a GPU, call a write API, or compare weights.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import uuid


class DockerCache:
    def __init__(self, container, deployment_id):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', container):
            raise ValueError('invalid container name')
        self.container = container
        self.root = '/serving-cache/' + str(uuid.UUID(deployment_id)) + '/'

    def command(self, *args):
        result = subprocess.run(['docker', 'exec', self.container, *args],
                                capture_output=True, timeout=45)
        if result.returncode:
            detail = result.stderr.decode(errors='replace').strip()[:500]
            raise RuntimeError('cache read failed: ' + detail)
        return result.stdout

    def prefix(self, relative, length):
        if relative.startswith('/') or '..' in relative.split('/'):
            raise ValueError('unsafe cache path')
        return self.command('head', '-c', str(length), self.root + relative)

    def read_json(self, relative):
        data = self.prefix(relative, (64 << 20) + 1)
        if len(data) > 64 << 20:
            raise ValueError('JSON exceeds 64 MiB: ' + relative)
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError('expected JSON object: ' + relative)
        return value, hashlib.sha256(data).hexdigest()

    def optional_json(self, relative):
        check = subprocess.run(['docker', 'exec', self.container, 'test', '-f', self.root + relative],
                               capture_output=True, timeout=15)
        if check.returncode == 1:
            return {'available': False}
        if check.returncode:
            raise RuntimeError('cannot inspect cached metadata: ' + relative)
        value, digest = self.read_json(relative)
        return {'available': True, 'sha256': digest, 'value': value}

    def weights_header(self, relative):
        prefix = self.prefix(relative, 8)
        if len(prefix) != 8:
            raise ValueError('missing safetensors length: ' + relative)
        length = struct.unpack('<Q', prefix)[0]
        if not 0 < length <= 16 << 20:
            raise ValueError('invalid safetensors header length: ' + relative)
        data = self.prefix(relative, 8 + length)
        if len(data) != 8 + length:
            raise ValueError('truncated safetensors header: ' + relative)
        header = json.loads(data[8:])
        tensors = {name: value for name, value in header.items() if name != '__metadata__'}
        return {
            'file': relative, 'header_sha256': hashlib.sha256(data[8:]).hexdigest(),
            'tensor_count': len(tensors),
            'lora_tensor_count': sum('.lora_' in name for name in tensors),
            'embedding_tensors': [
                {'name': name, 'dtype': value.get('dtype'), 'shape': value.get('shape')}
                for name, value in sorted(tensors.items())
                if ('embed_tokens' in name or 'lm_head' in name) and '.lora_' not in name
            ],
        }


def token_index(tokens):
    result = {}
    for token in tokens:
        if not isinstance(token, dict) or type(token.get('id')) is not int:
            raise ValueError('added_tokens contains a non-object or invalid ID')
        result.setdefault(token['id'], []).append(token)
    return result


def compare_added_tokens(base, adapter):
    left, right = token_index(base), token_index(adapter)
    changes, fields = [], Counter()
    for token_id in sorted(set(left) | set(right)):
        a, b = left.get(token_id, []), right.get(token_id, [])
        if a == b:
            continue
        if len(a) == len(b) == 1:
            changed = sorted(key for key in set(a[0]) | set(b[0])
                             if (key in a[0], a[0].get(key)) != (key in b[0], b[0].get(key)))
        else:
            changed = ['entry_count']
        fields.update(changed)
        changes.append({'id': token_id, 'changed_fields': changed, 'base': a, 'adapter': b})
    duplicate_base = sorted(key for key, values in left.items() if len(values) > 1)
    duplicate_adapter = sorted(key for key, values in right.items() if len(values) > 1)
    return {
        'base_count': len(base), 'adapter_count': len(adapter), 'array_equal': base == adapter,
        'records_equal_by_id': left == right,
        'only_array_order_differs': base != adapter and left == right and not duplicate_base and not duplicate_adapter,
        'base_duplicate_ids': duplicate_base, 'adapter_duplicate_ids': duplicate_adapter,
        'base_only_ids': sorted(set(left) - set(right)), 'adapter_only_ids': sorted(set(right) - set(left)),
        'changed_id_count': len(changes), 'changed_field_counts': dict(sorted(fields.items())),
        'changes': changes,
    }


def selected_config(result, keys):
    if not result['available']:
        return result
    return {'available': True, 'sha256': result['sha256'],
            'fields': {key: {'present': key in result['value'], 'value': result['value'].get(key)} for key in keys}}


def inspect_cache(cache):
    base, base_sha = cache.read_json('model/tokenizer.json')
    adapter, adapter_sha = cache.read_json('adapter-source/tokenizer.json')
    report = {
        'tokenizer_sha256': {'base': base_sha, 'adapter': adapter_sha},
        'tokenizer_fields_equal': {
            key: (key in base, base.get(key)) == (key in adapter, adapter.get(key))
            for key in ('model', 'normalizer', 'pre_tokenizer', 'post_processor', 'decoder', 'padding', 'truncation')
        },
        'added_tokens': compare_added_tokens(base.get('added_tokens', []), adapter.get('added_tokens', [])),
    }
    config_keys = ('bos_token', 'eos_token', 'pad_token', 'unk_token', 'add_bos_token', 'add_eos_token',
                   'tokenizer_class', 'additional_special_tokens', 'extra_special_tokens')
    report['tokenizer_config'] = {
        label: selected_config(cache.optional_json(directory + '/tokenizer_config.json'), config_keys)
        for label, directory in (('base', 'model'), ('adapter', 'adapter-source'))
    }
    report['base_config'] = selected_config(cache.optional_json('model/config.json'),
                                          ('model_type', 'vocab_size', 'tie_word_embeddings', 'hidden_size'))
    report['adapter_config'] = selected_config(cache.optional_json('adapter-source/adapter_config.json'),
                                             ('peft_type', 'task_type', 'r', 'lora_alpha', 'bias', 'target_modules',
                                              'modules_to_save', 'use_dora', 'use_rslora'))
    manifest, _ = cache.read_json('model/_manifest.json')
    names = [item['path'] for item in manifest['files'] if '/' not in item['path']
             and item['path'].startswith('model') and item['path'].endswith('.safetensors')]
    report['weight_headers'] = {
        'base': [cache.weights_header('model/' + name) for name in sorted(names)],
        'adapter': cache.weights_header('adapter-source/adapter_model.safetensors'),
        'tensor_values_compared': False,
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment-id', required=True)
    parser.add_argument('--container', default='model-deployment')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if Path.cwd().resolve() != root:
        parser.error('run from ~/workspace/agent_project/model_platform')
    deployment_id = str(uuid.UUID(args.deployment_id))
    cache = DockerCache(args.container, deployment_id)
    report = inspect_cache(cache)
    report.update(deployment_id=deployment_id, cache_path=cache.root,
                  note='Diagnostic metadata only. No model files modified; tensor values not compared.')
    out = root / 'tests' / 'results' / 'serving' / (deployment_id + '-inspect-' + str(uuid.uuid4()) + '.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    summary = dict(report['added_tokens'])
    summary['changes'] = summary['changes'][:20]
    print(json.dumps({'added_tokens': summary, 'tokenizer_fields_equal': report['tokenizer_fields_equal'],
                      'weight_headers': report['weight_headers']}, ensure_ascii=False, indent=2))
    print('[report] ' + str(out), flush=True)
    print('Inspection complete; serving compatibility has not been established.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise SystemExit('INSPECTION FAILED: ' + str(exc)) from exc
