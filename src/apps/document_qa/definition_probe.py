"""Read four pinned Agent resources without inference or local state writes.

This observes definitions, not the active payload/template or answer quality.
It also works from a verified temporary copy with the repository on PYTHONPATH.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time

from src.apps.document_qa import readiness
from src.apps.document_qa.contracts import ContractError, digest, integer, uid
from src.apps.document_qa.intake import UploadError
from src.apps.document_qa.qa_agent_results import DEFINITION_FIELDS, definition_checked, question_checked
from src.apps.document_qa.qa_agent_transport import AgentQAGateway
from src.apps.document_qa.workflows import canonical

SOURCE_FILES = (
    'src/platform/agent/runtime/agent_runtime/agents.py',
    'src/platform/model/gateway/app/chat.go',
    'src/apps/document_qa/qa_agent_transport.py',
)
OUTPUT_REQUIRED = {'answer', 'outcome', 'citations', 'warnings', 'answer_truncated',
                   'tool_call_id', 'identity'}
ERROR_CODES = frozenset({
    'ARGUMENTS_INVALID', 'BASELINE_INVALID', 'READ_NOT_ALLOWED', 'RUN_IDENTITY_INVALID',
    'RUN_FAILURE_CHANGED', 'VERSION_IDENTITY_INVALID', 'SCHEMA_INVALID',
    'DEFINITION_COMPARISON_INVALID', 'LOCAL_SOURCE_UNAVAILABLE',
    'SETTINGS_REQUIRED', 'SETTINGS_INVALID', 'CONFIG_INVALID', 'CONFIG_UNAVAILABLE',
    'CONNECTION_INVALID', 'CREDENTIAL_NOT_CONFIGURED', 'CREDENTIAL_UNAVAILABLE',
    'AGENT_CONNECTION_DISABLED', 'AGENT_AUTH_UNAVAILABLE', 'AGENT_AUTH_REQUIRED',
    'AGENT_REDIRECT_REFUSED', 'AGENT_RESOURCE_UNAVAILABLE', 'AGENT_TOOL_CONFLICT',
    'AGENT_TOOL_HTTP_UNCONFIRMED', 'AGENT_TOOL_RESPONSE_INVALID',
    'AGENT_TOOL_RESPONSE_TOO_LARGE', 'AGENT_TOOL_OUTCOME_UNKNOWN',
    'AGENT_TOOL_JSON_INVALID', 'AGENT_TOOL_BUDGET_EXCEEDED',
    'QA_AGENT_DEFINITION_INVALID', 'QUESTION_INVALID', 'QUESTION_TOO_LARGE',
    'DEFINITION_PROBE_UNCONFIRMED',
})


class ProbeError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ProbeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default includes untrusted argv values in its stderr.
        raise ProbeError('ARGUMENTS_INVALID')


def need(ok, code):
    if not ok:
        raise ProbeError(code)


def baseline_checked(value):
    need(isinstance(value, dict) and type(value.get('schema')) is int
         and value['schema'] == 1, 'BASELINE_INVALID')
    result = {}
    for label, key in (('original', 'PC_question'), ('candidate', 'failed_prompt_trial')):
        row = value.get(key)
        need(isinstance(row, dict) and uid(row.get('agent_run_id'))
             and uid(row.get('agent_version_id')), 'BASELINE_INVALID')
        result[label] = {'run_id': row['agent_run_id'], 'version_id': row['agent_version_id']}
    need(result['original']['run_id'] != result['candidate']['run_id']
         and result['original']['version_id'] != result['candidate']['version_id'], 'BASELINE_INVALID')
    models = value.get('models')
    chat = models.get('chat') if isinstance(models, dict) else None
    need(isinstance(chat, dict) and uid(chat.get('deployment_id'))
         and uid(chat.get('model_version_id')), 'BASELINE_INVALID')
    result['chat'] = {k: chat[k] for k in ('deployment_id', 'model_version_id')}
    return result


def load_baseline(path):
    try:
        return baseline_checked(readiness.decode(readiness.read_file(path, 2 << 20)))
    except (readiness.ReadinessError, ContractError, OSError):
        raise ProbeError('BASELINE_INVALID') from None


def allowed_paths(baseline):
    return frozenset('/api/' + family + '/' + baseline[label][field]
                     for label in ('original', 'candidate')
                     for family, field in (('agent-runs', 'run_id'), ('agent-versions', 'version_id')))


class FixedReader(AgentQAGateway):
    def __init__(self, row, paths, clock=time.monotonic):
        need(len(paths) == 4 and all(isinstance(p, str) and re.fullmatch(
            r'/api/(?:agent-runs|agent-versions)/[a-f0-9-]{36}', p) for p in paths), 'BASELINE_INVALID')
        super().__init__(row, execute=False, allow_inference=False, timeout=8,
                         budget_seconds=60, clock=clock)
        self.paths, self.used_paths = frozenset(paths), set()

    def request(self, method, path, body=None, key=None):
        need(method == 'GET' and body is None and key is None and path in self.paths
             and path not in self.used_paths and self.calls < 4, 'READ_NOT_ALLOWED')
        self.used_paths.add(path)
        return super().request(method, path, body, key)


def checked_input(value):
    need(isinstance(value, dict) and {'question'} <= set(value) <= {'question', 'filters'},
         'RUN_IDENTITY_INVALID')
    question_checked(value['question'])
    need('filters' not in value or isinstance(value['filters'], dict), 'RUN_IDENTITY_INVALID')
    return value


def checked_version(value, expected, run, state):
    need(isinstance(value, dict) and value.get('agent_version_id') == expected['version_id']
         and uid(value.get('agent_id')) and value.get('state') == state
         and integer(value.get('revision'), 1) and integer(value.get('version'), 1, 100)
         and digest(value.get('definition_sha256'))
         and value['definition_sha256'] == run['definition_sha256'], 'VERSION_IDENTITY_INVALID')
    definition = {k: value[k] for k in DEFINITION_FIELDS if k in value}
    definition_checked(definition)
    inp, out = value.get('input_schema'), value.get('output_schema')
    need(isinstance(inp, dict) and inp.get('type') == 'object'
         and inp.get('required') == ['question'] and inp.get('additionalProperties') is False
         and isinstance(inp.get('properties'), dict)
         and isinstance(inp['properties'].get('question'), dict)
         and inp['properties']['question'].get('type') == 'string'
         and isinstance(out, dict) and out.get('type') == 'object'
         and isinstance(out.get('required'), list)
         and all(isinstance(k, str) for k in out['required'])
         and OUTPUT_REQUIRED <= set(out['required']), 'SCHEMA_INVALID')
    return definition


def probe(reader, baseline):
    runs, versions, definitions = {}, {}, {}
    for label, state in (('original', 'published'), ('candidate', 'draft')):
        expected = baseline[label]
        run = reader.get('/api/agent-runs/' + expected['run_id'])
        need(isinstance(run, dict) and run.get('agent_run_id') == expected['run_id']
             and run.get('agent_version_id') == expected['version_id']
             and digest(run.get('definition_sha256')), 'RUN_IDENTITY_INVALID')
        error = run.get('error')
        need(run.get('state') == 'failed' and run.get('result') is None
             and isinstance(error, dict) and error.get('code') == 'ANSWER_REFERENCE_INVALID'
             and error.get('stage') == 'finalize', 'RUN_FAILURE_CHANGED')
        checked_input(run.get('input'))
        version = reader.get('/api/agent-versions/' + expected['version_id'])
        definitions[label] = checked_version(version, expected, run, state)
        runs[label], versions[label] = run, version
    first, second = definitions['original'], definitions['candidate']
    changes = sorted(k for k in DEFINITION_FIELDS if canonical(first[k]) != canonical(second[k]))
    need(versions['original']['agent_id'] == versions['candidate']['agent_id']
         and canonical(runs['original']['input']) == canonical(runs['candidate']['input'])
         and canonical(versions['original']['input_schema']) == canonical(versions['candidate']['input_schema'])
         and canonical(versions['original']['output_schema']) == canonical(versions['candidate']['output_schema'])
         and all(first['model'][k] == v for k, v in baseline['chat'].items())
         and changes == ['prompt'], 'DEFINITION_COMPARISON_INVALID')
    report = {
        'schema': 1, 'status': 'OBSERVED', 'scope': 'pinned_agent_definition_metadata_only',
        'run_error_code': 'ANSWER_REFERENCE_INVALID', 'run_error_stage': 'finalize',
        'both_runs_still_failed': True, 'run_version_hashes_match': True,
        'definition_hash_recomputed': False, 'same_agent': True, 'same_input': True,
        'model_pin_matches_baseline': True, 'changed_definition_fields': changes,
        'definitions': {}, 'steps_rechecked': False, 'reference_diagnostics_rechecked': False,
        'active_runtime_verified': False, 'payload_verified': False, 'template_verified': False,
        'head_readback_verified': False, 'answer_quality_verified': False,
        'f0a_accepted': False, 'inference_requests': 0, 'write_requests': 0,
    }
    for label, definition in definitions.items():
        prompt = definition['prompt'].encode('utf-8')
        report['definitions'][label] = {
            'state': versions[label]['state'], 'prompt_bytes': len(prompt),
            'prompt_sha256': hashlib.sha256(prompt).hexdigest(),
            **{k: definition[k] for k in ('max_tokens', 'temperature', 'context_max_chars', 'timeout_ms')},
        }
    return report


def local_source_hashes(root):
    try:
        return {name: hashlib.sha256(readiness.read_file(root / name)).hexdigest()
                for name in SOURCE_FILES}
    except (readiness.ReadinessError, OSError):
        raise ProbeError('LOCAL_SOURCE_UNAVAILABLE') from None


def main(argv=None):
    reader, stage = None, 'arguments'
    try:
        parser = ProbeParser(description=__doc__)
        parser.add_argument('--project-root', type=Path, default=Path.cwd())
        parser.add_argument('--settings', type=Path,
                            default=Path.home() / '.local/state/agent-project-document-qa/settings.json')
        parser.add_argument('--baseline', type=Path,
                            default=Path('docs/testing/document-qa-live-observed-20261012.json'))
        args = parser.parse_args(argv)
        stage = 'settings'
        settings = args.settings.expanduser()
        # A missing/raced settings file can never fall back to an Ops config.
        try:
            readiness.read_file(settings)
        except readiness.ReadinessError:
            raise ProbeError('SETTINGS_REQUIRED') from None
        config, _, provenance = readiness.load_settings(settings, settings)
        need(provenance == 'document_qa_settings', 'SETTINGS_INVALID')
        stage = 'baseline'
        path = args.baseline if args.baseline.is_absolute() else args.project_root / args.baseline
        baseline = load_baseline(path)
        stage = 'local_sources'
        sources = local_source_hashes(args.project_root)
        stage = 'agent_gets'
        reader = FixedReader(config['platforms']['agent'], allowed_paths(baseline))
        report = probe(reader, baseline)
        report.update(get_requests=reader.calls, repository_source_sha256=sources,
                      repository_sources_are_active_runtime_evidence=False)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    except Exception as exc:
        # The CLI boundary must not emit private exception text or tracebacks.
        # Pure helpers still raise errors; only this report boundary masks them.
        code = getattr(exc, 'code', None)
        reason = code if isinstance(code, str) and code in ERROR_CODES else 'DEFINITION_PROBE_UNCONFIRMED'
        print(json.dumps({'schema': 1, 'status': 'BLOCKED', 'scope': 'pinned_agent_definition_metadata_only',
                          'reason': reason, 'stage': stage,
                          'get_requests': reader.calls if reader else 0,
                          'inference_requests': 0, 'write_requests': 0, 'f0a_accepted': False}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
