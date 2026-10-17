"""One candidate-count semantic mutant; expected designated FAIL1/ERROR0.

Only new D29 helpers are loaded. The old prompt mutant is never imported/run.
"""
import importlib.util
from pathlib import Path
import re
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.dont_write_bytecode = True
_path = Path(__file__).with_name('test_codex_owner_message_correlation.py')
_spec = importlib.util.spec_from_file_location('_d29_message_helpers', _path)
tests = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tests)
REPORT = None


def save(name, value):
    path = tests.private_write(REPORT / name, tests.q.canonical(value))
    print('RESULT_FILE=' + str(path), flush=True)


def candidate_mutant():
    source_path = tests.ROOT / 'src/ops/tools/codex_owner_turns.py'
    source = source_path.read_text()
    start = source.index('def match_task_turn(')
    following = re.search(r'^def [A-Za-z_]\w*\(', source[start + 1:], re.MULTILINE)
    end = start + 1 + following.start() if following else len(source)
    body = source[start:end]
    predicate = re.compile(r'len\((candidates|matches)\)\s*==\s*1')
    locations = list(predicate.finditer(body))
    if len(locations) != 1:
        raise RuntimeError('EXACTLY_ONE_CANDIDATE_COUNT_PREDICATE_REQUIRED')
    found = locations[0]
    replacement = found.group(0).replace('==', '>=', 1)
    mutant_source = source[:start] + body[:found.start()] + replacement + body[found.end():] + source[end:]
    module = types.ModuleType('src.ops.tools._d29_candidate_count_mutant')
    module.__package__ = 'src.ops.tools'
    module.__file__ = str(source_path)
    exec(compile(mutant_source, str(source_path) + ':candidate-count-only', 'exec'), module.__dict__)
    return module, {'source_sha256': tests.q.sha(source.encode('utf8')),
                    'mutant_sha256': tests.q.sha(mutant_source.encode('utf8')),
                    'before': found.group(0), 'after': replacement}


class CandidateCountMutant(unittest.TestCase):
    def test_two_exact_messages_cannot_create_a_terminal(self):
        module, mutation = candidate_mutant()
        f = tests.AutoFixture().start_auto()
        owner = f.owner(tests.duplicate_exact_pages(f))
        # Support either a module-qualified matcher call or a direct import.
        with mock.patch.object(tests.turns, 'match_task_turn', module.match_task_turn), \
             mock.patch.object(tests.q, 'match_task_turn', module.match_task_turn, create=True):
            observed = f.observe_auto(owner)
        terminal = f.folder / 'terminal.json'
        binding = f.folder / 'automatic-binding.json'
        witness = dict(mutation, mutation='candidate-count-equals-one-to-at-least-one-only',
                       temporary_root=str(f.root), observed=observed,
                       complete_history_page_count=len(owner.calls),
                       distinct_turn_ids=['turn-first', 'turn-second'],
                       terminal_record_written=terminal.exists(),
                       terminal_sha256=tests.q.sha(terminal.read_bytes()) if terminal.exists() else None,
                       binding_record_written=binding.exists(), actual_codex_queue=0, native_rpc=0)
        save('mutant-witness.json', witness)
        if observed.get('status') != 'TURN_COMPLETED' or not terminal.exists() or not binding.exists() or len(owner.calls) != 2:
            raise RuntimeError('MUTANT_WRONG_TERMINAL_WITNESS_NOT_CONFIRMED')
        self.assertNotEqual(observed['status'], 'TURN_COMPLETED',
                            'MUTANT_ACCEPTED_TWO_EXACT_MESSAGES_TERMINAL')


def main():
    global REPORT
    REPORT = Path(tempfile.mkdtemp(prefix='agent-message-correlation-mutant-', dir='/tmp'))
    REPORT.chmod(0o700)
    with tests.offline_guards():
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(CandidateCountMutant)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    detected = (result.testsRun == 1 and len(result.failures) == 1 and not result.errors
                and not result.skipped
                and 'MUTANT_ACCEPTED_TWO_EXACT_MESSAGES_TERMINAL' in result.failures[0][1])
    save('mutant-result.json', {'run': result.testsRun, 'failures': len(result.failures),
                              'errors': len(result.errors), 'skips': len(result.skipped),
                              'designated_assertion_detected': detected,
                              'actual_codex_queue': 0, 'native_rpc': 0})
    return 1 if detected else 2


if __name__ == '__main__':
    raise SystemExit(main())
