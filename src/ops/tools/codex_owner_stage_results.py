"""Capture fixed local tests and retain supplied review/publisher outcomes.

No owner RPC, queue, credentials, signing keys or required-check publisher.
Tool-response origin and reviewer independence are deliberately not certified.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid

from . import agent_execution_package as files

TEST_FILE = 'src/ops/tools/tests/test_codex_owner_message_correlation.py'
TEST_IDS = tuple('MessageCorrelationAcceptance.test_M%02d' % n for n in range(1, 8))
MAX_BYTES = 1048576
_RUNTIME_MODULES = ('github_check_audit','github_fixture_collector','github_fixture_ledger',
                    'github_fixture_controller','github_fixture_config','codex_readiness',
                    'codex_daemon_websocket','codex_unattended_admission',
                    'codex_unattended_intent_journal','codex_unattended_queue_observer',
                    'codex_unattended_quota_reader','codex_unattended_submission_binding',
                    'codex_unattended_queue_receipts','codex_unattended_owner_ack_adapter',
                    'agent_execution_package','codex_owner_cli_controller','codex_owner_turns',
                    'codex_owner_stage_results')
REQUIRED_SOURCE_PATHS = frozenset(
    ['src/ops/tools/'+name+'.py' for name in _RUNTIME_MODULES]
    + ['src/__init__.py','src/ops/__init__.py', TEST_FILE,
       'src/ops/tools/build_agent_execution_package.py',
       'src/ops/tools/tests/test_codex_owner_cli_controller.py',
       'src/ops/tools/tests/test_codex_owner_message_correlation_mutant.py',
       'src/ops/tools/tests/fixtures/codex_unattended_admission/flex_gate.py.txt',
       'src/ops/tools/tests/fixtures/codex_unattended_intent_journal/synthetic.json'])


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def shape(value, names):
    return type(value) is dict and set(value) == set(names)


def hexpin(value, length=64):
    return type(value) is str and len(value) == length and all(c in '0123456789abcdef' for c in value)


def strict_json(raw):
    value = files.decode(raw)
    def check(node, depth=0):
        files.need(depth <= 32, 'STAGE_JSON_TOO_DEEP')
        if type(node) is float:
            files.need(math.isfinite(node), 'STAGE_JSON_NONFINITE')
        elif type(node) in (dict,list):
            for child in (node.values() if type(node) is dict else node):
                check(child,depth+1)
    check(value)
    return value


class StageResults:
    def __init__(self, controller, pin, request_pin, context, source_manifest_bytes):
        self.controller, self.uid = controller, controller.uid
        with files.lock(controller.records, self.uid):
            folder, request, row = controller._request(pin, request_pin)
            intent = files.decode(row['intent_bytes'])
            terminal = files.decode(controller_read(folder / 'terminal.json', self.uid))
            files.need(terminal.get('status') == 'TURN_COMPLETED'
                       and terminal.get('request_sha256') == request_pin, 'WORKFLOW_TURN_NOT_COMPLETED')
            files.need(shape(context, ('attempt_id','repository','base_sha','head_sha','plan_sha256','source_manifest_sha256')),
                       'STAGE_CONTEXT_INVALID')
            try:
                identity = uuid.UUID(context['attempt_id'])
            except Exception:
                raise files.PackageError('STAGE_ATTEMPT_INVALID') from None
            files.need(str(identity) == context['attempt_id'] and identity.version == 4
                       and context['repository'] == intent['repository'] and context['base_sha'] == intent['base_sha']
                       and context['plan_sha256'] == intent['plan_sha256'] and hexpin(context['head_sha'],40), 'STAGE_CONTEXT_MISMATCH')
            files.need(type(source_manifest_bytes) is bytes and 1 <= len(source_manifest_bytes) <= MAX_BYTES
                       and sha(source_manifest_bytes) == context['source_manifest_sha256'], 'SOURCE_MANIFEST_PIN_MISMATCH')
            manifest = files.decode(source_manifest_bytes)
            files.need(type(manifest) is dict and REQUIRED_SOURCE_PATHS <= set(manifest)
                       and len(manifest) <= 64 and canonical(manifest) == source_manifest_bytes
                       and all(type(p) is str and '..' not in Path(p).parts
                               and (p in ('src/__init__.py','src/ops/__init__.py')
                                    or (p.startswith('src/ops/tools/') and p.endswith(('.py','.json','.txt'))))
                               and hexpin(h) for p,h in manifest.items()), 'SOURCE_MANIFEST_INVALID')
            self.context = dict(context, task_id=row['task_id'], intent_sha256=pin, request_sha256=request_pin)
            self.manifest, self.request = manifest, request
            self.directory = folder / 'stages' / context['attempt_id']
            for p in (folder / 'stages', self.directory):
                try:
                    p.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                files.directory(p, self.uid)
            controller_write(self.directory/'context.json', canonical(self.context), self.uid)
            controller_write(self.directory/'source-manifest.json', source_manifest_bytes, self.uid)
            self.terminal_sha256 = sha(controller_read(folder/'terminal.json', self.uid))

    def _record(self, stage, raw, status, origin, previous, details):
        files.need(type(raw) is bytes and len(raw) <= MAX_BYTES, 'STAGE_ARTIFACT_INVALID')
        record = {'schema':1,'stage':stage,'context':self.context,'status':status,'origin':origin,
                  'artifact_sha256':sha(raw),'artifact_bytes':len(raw),'previous_stage_sha256':previous,
                  'details':details,'controller_fixture':self.controller.fixture,
                  'input_origin_verified':False,'origin_verified':False,'independent_review_verified':False,
                  'trusted_gate_verified':False,'native_workflow_verified':False,
                  'native_ready':False,'merge_authorized':False,'replay_permitted':False}
        controller_write(self.directory/(stage+'.artifact'), raw, self.uid)
        controller_write(self.directory/(stage+'.json'), canonical(record), self.uid)
        return dict(record, record_sha256=sha(canonical(record)))

    def read(self, stage):
        files.need(stage in ('tests','reviewer','draft_pr'), 'STAGE_INVALID')
        files.need((self.directory/(stage+'.json')).is_file(), 'STAGE_NOT_RECORDED')
        raw = controller_read(self.directory/(stage+'.json'), self.uid)
        record = files.decode(raw)
        artifact = controller_read(self.directory/(stage+'.artifact'), self.uid)
        files.need(canonical(record) == raw and record['context'] == self.context
                   and record['stage'] == stage and record['artifact_sha256'] == sha(artifact)
                   and record['artifact_bytes'] == len(artifact), 'STAGE_RECORD_MISMATCH')
        expected_previous = (self.terminal_sha256 if stage == 'tests' else
                             self.read('tests' if stage == 'reviewer' else 'reviewer')['record_sha256'])
        files.need(record['previous_stage_sha256'] == expected_previous
                   and all(record[k] is False for k in ('input_origin_verified','independent_review_verified',
                           'trusted_gate_verified','native_workflow_verified','native_ready','merge_authorized',
                           'replay_permitted')), 'STAGE_CHAIN_MISMATCH')
        if stage == 'tests':
            for name in ('stdout','stderr'):
                data = controller_read(self.directory/(name+'.bin'),self.uid)
                files.need(record['details']['output'][name] == {'sha256':sha(data),'bytes':len(data)},
                           'TEST_OUTPUT_MISMATCH')
        return dict(record, record_sha256=sha(raw))

    def _source_bytes(self, repository):
        repository = Path(repository)
        allowed = self.controller.records.parent if self.controller.fixture else files.APPROVED_WORKSPACE/'var'
        files.need(repository.is_absolute() and repository.resolve().is_relative_to(allowed.resolve()),
                   'TEST_WORKSPACE_INVALID')
        for path, pin in self.manifest.items():
            source = repository / path
            files.need(source.resolve(strict=True).is_relative_to(repository.resolve())
                       and sha(source.read_bytes()) == pin, 'TEST_SOURCE_PIN_MISMATCH')
        files.need(TEST_FILE in self.manifest, 'TEST_RUNNER_NOT_PINNED')
        return repository

    def capture_tests(self, repository, test_ids=TEST_IDS):
        """Only this method can assign DIRECT_LOCAL_PROCESS_CAPTURE.

        A fixture or supplied artifact has no API for selecting this origin.
        Fixed source pins and fixed test IDs bound the process, not a callback.
        """
        with files.lock(self.controller.records, self.uid):
            if (self.directory/'tests.json').exists():
                return self.read('tests')
            files.need(type(test_ids) in (tuple,list) and 1 <= len(test_ids) <= 7
                       and len(set(test_ids)) == len(test_ids) and all(x in TEST_IDS for x in test_ids), 'TEST_SELECTION_INVALID')
            repository = self._source_bytes(repository)
            result_path, context_path = self.directory/'test-result.json', self.directory/'runner-context.json'
            files.need(not result_path.exists() and not (self.directory/'capture-attempt.json').exists(), 'TEST_CAPTURE_UNKNOWN_NO_REPLAY')
            controller_write(context_path, canonical(self.context), self.uid)
            argv = ['/usr/bin/python3','-I','-S','-B',str(repository/TEST_FILE),
                    '--context',str(context_path),'--result',str(result_path)] + list(test_ids)
            controller_write(self.directory/'capture-attempt.json', canonical({'argv':argv,'cwd':str(repository)}), self.uid)
            started, child, code, complete, timed_out = time.time(), None, None, False, False
            streams = {}
            try:
                for name in ('stdout','stderr'):
                    fd = os.open(self.directory/(name+'.bin'), os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                    streams[name] = os.fdopen(fd,'wb')
                child = subprocess.Popen(argv, cwd=str(repository), stdin=subprocess.DEVNULL,
                                         stdout=streams['stdout'],stderr=streams['stderr'],shell=False,
                                         close_fds=True,start_new_session=True)
                try:
                    code = child.wait(timeout=60)
                    complete = True
                except subprocess.TimeoutExpired:
                    timed_out = True
            except Exception:
                complete = False
            finally:
                if child is not None:
                    try:
                        if not complete:
                            os.killpg(child.pid,signal.SIGKILL)
                        child.wait(timeout=1)
                    except Exception:
                        complete = False
                for stream in streams.values():
                    stream.flush(); os.fsync(stream.fileno()); stream.close()
            artifact = controller_read(result_path,self.uid) if result_path.exists() else b''
            statistics, valid = None, False
            try:
                statistics = files.decode(artifact)
                valid = (shape(statistics,('schema','stage','context','test_ids','run','failures','errors','skips'))
                         and type(statistics['schema']) is int and statistics['schema'] == 1
                         and statistics['stage'] == 'tests' and statistics['context'] == self.context
                         and statistics['test_ids'] == list(test_ids)
                         and all(type(statistics[k]) is int and statistics[k] >= 0 for k in ('run','failures','errors','skips'))
                         and statistics['run'] == len(test_ids)
                         and all(statistics[k] == 0 for k in ('failures','errors','skips')))
            except Exception:
                pass
            output = {}
            for name in ('stdout','stderr'):
                p = self.directory/(name+'.bin')
                data = controller_read(p,self.uid) if p.exists() else b''
                output[name] = {'sha256':sha(data),'bytes':len(data)}
            passed = valid and complete and not timed_out and type(code) is int and code == 0
            try:
                self._source_bytes(repository)
            except Exception:
                passed = False
                valid = False
            details = {'returncode':code,'complete':complete,'timed_out':timed_out,'started_at':started,
                       'completed_at':time.time(),'test_ids':list(test_ids),'statistics_valid':valid,
                       'output':output,'argv':argv,'cwd':str(repository),'all_seven_selected':tuple(test_ids)==TEST_IDS}
            return self._record('tests',artifact,'TESTS_PASSED' if passed else 'TESTS_FAILED',
                                'DIRECT_LOCAL_PROCESS_CAPTURE',self.terminal_sha256,details)

    def record_reviewer(self, raw, execution_ref, author_ref):
        with files.lock(self.controller.records,self.uid):
            tests = self.read('tests')
            files.need(tests['status'] == 'TESTS_PASSED' and tests['details']['all_seven_selected'], 'TESTS_NOT_COMPLETE')
            files.need(type(raw) is bytes and 1 <= len(raw) <= MAX_BYTES, 'REVIEW_ARTIFACT_INVALID')
            value = strict_json(raw)
            files.need(shape(value,('schema','stage','context','decision','tests_artifact_sha256','findings'))
                       and type(value['schema']) is int and value['schema'] == 1 and value['stage'] == 'reviewer'
                       and value['context'] == self.context and value['decision'] in ('PASS','REQUEST_CHANGES')
                       and value['tests_artifact_sha256'] == tests['artifact_sha256']
                       and type(value['findings']) is list and len(value['findings']) <= 64,
                       'REVIEW_CONTEXT_MISMATCH')
            files.need(all(type(x) is str and 1 <= len(x) <= 2048 for x in value['findings'])
                       and type(execution_ref) is str and 1 <= len(execution_ref) <= 256
                       and type(author_ref) is str and 1 <= len(author_ref) <= 256
                       and execution_ref != author_ref, 'REVIEW_EXECUTION_REF_INVALID')
            passed = value['decision'] == 'PASS' and not value['findings']
            return self._record('reviewer',raw,'REVIEW_PASSED' if passed else 'REVIEW_PENDING',
                                'SUPPLIED_TOOL_RESPONSE',tests['record_sha256'],
                                {'execution_ref':execution_ref,'author_ref':author_ref,'decision':value['decision']})

    def record_draft(self, raw, *, expected_head_sha, published_sources,
                     expected_pr_number=None, expected_base_branch=None, expected_head_branch=None):
        with files.lock(self.controller.records,self.uid):
            review = self.read('reviewer')
            files.need(review['status'] == 'REVIEW_PASSED', 'REVIEW_NOT_PASSED')
            files.need(type(raw) is bytes and 1 <= len(raw) <= MAX_BYTES and hexpin(expected_head_sha,40), 'DRAFT_ARTIFACT_INVALID')
            files.need(type(expected_pr_number) is int and expected_pr_number > 0
                       and type(expected_base_branch) is str and 1 <= len(expected_base_branch) <= 256
                       and type(expected_head_branch) is str and 1 <= len(expected_head_branch) <= 256,
                       'DRAFT_EXPECTED_TARGET_REQUIRED')
            value = strict_json(raw)
            allowed = {'url','number','state','merged','mergeable','draft','body','title','base','base_sha','head','head_sha',
                       'head_repo_id','head_repo_full_name','merge_commit_sha','user','requested_reviewers','requested_team_reviewers',
                       'diff','comments','created_at','updated_at','closed_at','merged_at','commits','changed_files','additions',
                       'deletions','display_url','display_title'}
            files.need(type(value) is dict and set(value) <= allowed
                       and {'url','number','state','draft','base','base_sha','head','head_sha','head_repo_full_name'} <= set(value),
                       'DRAFT_SCHEMA_INVALID')
            for name in ('number','head_repo_id','commits','changed_files','additions','deletions'):
                if name in value and value[name] is not None:
                    files.need(type(value[name]) is int and value[name] >= 0, 'DRAFT_FIELD_TYPE_INVALID')
            for name in ('draft','merged','mergeable'):
                if name in value and value[name] is not None:
                    files.need(type(value[name]) is bool, 'DRAFT_FIELD_TYPE_INVALID')
            for name in ('url','state','body','title','base','base_sha','head','head_sha','head_repo_full_name',
                         'merge_commit_sha','created_at','updated_at','closed_at','merged_at','display_url','display_title','diff'):
                if name in value and value[name] is not None:
                    files.need(type(value[name]) is str, 'DRAFT_FIELD_TYPE_INVALID')
            if value.get('user') is not None:
                files.need(type(value['user']) is dict, 'DRAFT_FIELD_TYPE_INVALID')
            for name in ('requested_reviewers','requested_team_reviewers','comments'):
                if value.get(name) is not None:
                    files.need(type(value[name]) is list, 'DRAFT_FIELD_TYPE_INVALID')
            files.need(type(published_sources) is dict and set(published_sources) == set(self.manifest)
                       and all(type(data) is bytes and len(data) <= MAX_BYTES
                               and sha(data) == self.manifest[path] for path,data in published_sources.items()),
                       'PUBLISHED_SOURCE_MISMATCH')
            matched = (type(value['number']) is int and value['number'] == expected_pr_number and value['state'] == 'open'
                       and value['draft'] is True and value.get('merged',False) is False
                       and value['base_sha'] == self.context['base_sha'] and value['head_sha'] == expected_head_sha
                       and value['head_repo_full_name'] == self.context['repository']
                       and value['base'] == expected_base_branch and value['head'] == expected_head_branch
                       and value['url'] == 'https://github.com/'+self.context['repository']+'/pull/'+str(value['number']))
            return self._record('draft_pr',raw,'DRAFT_RECORDED' if matched else 'DRAFT_PENDING',
                                'SUPPLIED_TOOL_RESPONSE',review['record_sha256'],
                                {'tested_head_sha':self.context['head_sha'],'publication_head_sha':expected_head_sha,
                                 'published_source_hashes_match':True,'draft_fields_match':matched,
                                 'pr_number':value['number'],'url':value['url']})


# Lazy imports preserve a single implementation of the existing file protocol.
def controller_read(path, uid):
    from .codex_owner_cli_controller import durable_read
    return durable_read(path,uid)


def controller_write(path, raw, uid):
    from .codex_owner_cli_controller import write_once
    return write_once(path,raw,uid)
