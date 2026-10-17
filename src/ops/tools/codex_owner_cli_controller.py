"""At-most-once existing-owner CLI controller, disabled unless caller enables it.

CLI stdout is not a native ACK. The legacy intent clientUserMessageId is only a
local claim token here, never a native message ID. No fictional RPC params are
passed to native receipt/binding APIs. Caller owns DB, policy and live client.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time
import uuid

from . import agent_execution_package as files
from . import codex_unattended_intent_journal as journal
from . import github_check_audit as audit
from .codex_owner_turns import observe_bound_turn

MAX_STREAM = 65536
APPROVED_WORKSPACE = files.APPROVED_WORKSPACE


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('utf8')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def need(value, reason):
    files.need(value, reason)


def durable_read(path, uid):
    raw = files.read_file(path, uid)
    fd = os.open(path, files.FILE_FLAGS)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    fd = os.open(Path(path).parent, files.DIR_FLAGS)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return raw


def write_once(path, raw, uid):
    path = Path(path)
    files.directory(path.parent, uid)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        need(durable_read(path, uid) == raw, 'CONTROL_RECORD_CONFLICT')
        return
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    fd = os.open(path.parent, files.DIR_FLAGS)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def queue_once(argv, *, timeout=5):
    """One bounded CLI subprocess; callers must use controller authorization."""
    need(type(timeout) in (int, float) and 0 < timeout <= 5, 'CLI_TIMEOUT_INVALID')
    need(type(argv) is list and len(argv) == 11 and all(type(x) is str and '\x00' not in x for x in argv)
         and Path(argv[0]).is_absolute() and Path(argv[0]).name == 'codex'
         and argv[1:3] == ['queue', '--remote'] and argv[3].startswith('unix:///')
         and argv[4:8] == ['--approve-for-me','-C',str(APPROVED_WORKSPACE),'--thread']
         and str(uuid.UUID(argv[8])) == argv[8] and argv[9] == '--message'
         and 1 <= len(argv[10].encode('utf8')) <= 16384, 'CLI_ARGV_INVALID')
    buffers, reason, child = {'stdout': bytearray(), 'stderr': bytearray()}, 'CLI_RETURNED', None
    try:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, cwd='/', shell=False,
                                 close_fds=True, start_new_session=True)
    except OSError:
        return {'reason': 'SPAWN_FAILED', 'returncode': None, 'stdout': b'',
                'stderr_sha256': sha(b''), 'stderr_bytes': 0, 'streams_complete': False}
    try:
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            for name in ('stdout', 'stderr'):
                stream = getattr(child, name); os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    reason = 'CLI_TIMEOUT_UNKNOWN'; break
                for key, _mask in selector.select(min(left, 0.05)):
                    block = os.read(key.fileobj.fileno(), 4096)
                    if not block:
                        selector.unregister(key.fileobj); continue
                    data = buffers[key.data]; remaining = MAX_STREAM - len(data)
                    data.extend(block[:remaining])
                    if len(block) > remaining:
                        reason = 'CLI_STREAM_LIMIT_UNKNOWN'; break
                if reason != 'CLI_RETURNED':
                    break
            if reason == 'CLI_RETURNED':
                try:
                    child.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    reason = 'CLI_TIMEOUT_UNKNOWN'
    except Exception:
        reason = 'CLI_IO_UNKNOWN'
    finally:
        # Cleanup also covers selector construction/registration failures.
        try:
            if reason != 'CLI_RETURNED' or child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=1)
        except Exception:
            reason = 'CLI_IO_UNKNOWN'
        for stream in (child.stdout, child.stderr):
            with contextlib.suppress(Exception):
                stream.close()
    return {'reason': reason, 'returncode': child.returncode, 'stdout': bytes(buffers['stdout']),
            'stderr_sha256': sha(bytes(buffers['stderr'])), 'stderr_bytes': len(buffers['stderr']),
            'streams_complete': reason == 'CLI_RETURNED'}


class OwnerCLIController:
    def __init__(self, connection, records_dir, *, uid, cli_path, cli_entry_sha256,
                 role_threads, policy, schedule, owner_endpoint, submission_enabled=False, fixture=False):
        need(type(submission_enabled) is bool and type(fixture) is bool and type(uid) is int
             and uid == os.geteuid(), 'OWNER_SETTINGS_INVALID')
        need(type(role_threads) is dict and set(role_threads) == {'test_agent', 'reviewer'}
             and len(set(role_threads.values())) == 2
             and all(type(x) is str and str(uuid.UUID(x)) == x for x in role_threads.values()), 'ROLE_THREADS_INVALID')
        records = Path(records_dir)
        need(records.is_absolute() and ((not fixture and records.is_relative_to(APPROVED_WORKSPACE / 'owner-cli-state' / 'var'))
             or (fixture and records.is_relative_to(Path('/tmp')))), 'CONTROL_DIRECTORY_OUT_OF_SCOPE')
        self.records = files.directory(records, uid)
        self.connection, self.uid = connection, uid
        self.cli = Path(cli_path)
        need(self.cli.is_absolute() and self.cli.name == 'codex' and journal._hex(cli_entry_sha256), 'CLI_SETTINGS_INVALID')
        endpoint = Path(owner_endpoint)
        need(endpoint.is_absolute() and str(endpoint) == os.path.normpath(endpoint) and '..' not in endpoint.parts
             and '\x00' not in str(endpoint), 'OWNER_ENDPOINT_INVALID')
        if fixture:
            need(self.cli.is_relative_to(records.parent) and endpoint.is_relative_to(records.parent)
                 and self.cli.resolve(strict=True).is_relative_to(records.parent)
                 and endpoint.resolve(strict=False).is_relative_to(records.parent), 'FIXTURE_CLIENT_SCOPE_INVALID')
        self.owner_endpoint = str(endpoint)
        self.cli_pin, self.roles = cli_entry_sha256, dict(role_threads)
        self.policy, self.schedule = policy, json.loads(canonical(schedule))
        policy.validate_config(self.schedule)
        policies = [self.schedule['default']] + list(self.schedule['dates'].values())
        need(all(x['max_starts'] <= 3 and (fixture or (x['start'] == '23:00' and x['end'] == '07:00'))
                 for x in policies), 'SCHEDULE_APPROVAL_LIMIT_EXCEEDED')
        self.enabled, self.fixture = submission_enabled, fixture
        self.pause_paths = ([str(records / 'root-PAUSE'), str(records / 'user-PAUSE'), str(records / 'PAUSE')] if fixture else
                            ['/etc/codex-agent/PAUSE', '/home/codex_shshin/automation_mvp/PAUSE', str(records / 'PAUSE')])

    def settings(self):
        return {'schema': 1, 'project_id': 'agent_project', 'workspace': str(APPROVED_WORKSPACE),
                'records_dir': str(self.records), 'uid': self.uid, 'cli_path': str(self.cli),
                'cli_entry_sha256': self.cli_pin, 'role_threads': self.roles, 'owner_endpoint': self.owner_endpoint,
                'schedule': self.schedule, 'fixture': self.fixture, 'submission_enabled': self.enabled, 'local_token_scope': 'LOCAL_ONLY'}

    def _confirmed(self, result, reasons):
        report = result['report']
        need(report['reason'] in reasons and report['status'] == 'CONFIRMED'
             and report['database_commit_confirmed'] is True and report['transaction_closed'] is True
             and type(result.get('record')) is dict, 'INTENT_STORAGE_UNCONFIRMED')
        return result['record']

    def _prepare(self, raw, pin, role, prompt):
        self.policy.validate_config(self.schedule)
        need(all(x['max_starts'] <= 3 and (self.fixture or (x['start'] == '23:00' and x['end'] == '07:00'))
                 for x in [self.schedule['default']] + list(self.schedule['dates'].values())), 'SCHEDULE_APPROVAL_LIMIT_EXCEEDED')
        need(type(raw) is bytes and type(prompt) is str and '\x00' not in prompt and 1 <= len(prompt.encode('utf8')) <= 16384
             and role in self.roles, 'REQUEST_INVALID')
        value = files.decode(raw)
        need(value.get('project_id') == 'agent_project' and value.get('repository') == 'privateInt/agent_project'
             and value.get('dev_root') == str(APPROVED_WORKSPACE)
             and value.get('prompt_sha256') == sha(prompt.encode('utf8'))
             and value.get('settings_sha256') == sha(canonical(self.settings())), 'REQUEST_BINDING_MISMATCH')
        row = self._confirmed(journal.reserve_intent(self.connection, pin, raw), {'INTENT_STORED', 'INTENT_REUSED'})
        argv = [str(self.cli), 'queue', '--remote', 'unix://' + self.owner_endpoint, '--approve-for-me', '-C', str(APPROVED_WORKSPACE),
                '--thread', self.roles[role], '--message', prompt]
        request = {'schema': 1, 'role': role, 'thread_id': self.roles[role], 'intent_sha256': pin,
                   'settings_sha256': value['settings_sha256'], 'prompt': prompt, 'argv': argv,
                   'local_token_scope': 'LOCAL_ONLY'}
        data = canonical(request); folder = self.records / pin
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            pass
        files.directory(folder, self.uid)
        write_once(folder / 'intent-key.json', canonical([row[k] for k in ('project_id','batch_id','task_id')]), self.uid)
        write_once(folder / 'request.json', data, self.uid)
        return row, request, folder, sha(data)

    def prepare(self, raw, pin, role, prompt):
        with files.lock(self.records, self.uid):
            row, _request, _folder, request_pin = self._prepare(raw, pin, role, prompt)
            return {'status': row['state'], 'request_sha256': request_pin, 'local_token_scope': 'LOCAL_ONLY', 'native_ready': False}

    def submit(self, raw, pin, role, prompt, *, runner=queue_once):
        try:
            with files.lock(self.records, self.uid):
                row, request, folder, request_pin = self._prepare(raw, pin, role, prompt)
                if row['state'] != 'PREPARED':
                    return self.read(pin, request_pin)
                need(self.enabled, 'SUBMISSION_DISABLED')
                target = self.cli.resolve(strict=True)
                if self.fixture:
                    need(target.is_relative_to(self.records.parent)
                         and Path(self.owner_endpoint).resolve(strict=False).is_relative_to(self.records.parent), 'FIXTURE_CLIENT_SCOPE_INVALID')
                info = target.stat()
                need(stat.S_ISREG(info.st_mode) and not info.st_mode & 0o022 and info.st_nlink == 1
                     and info.st_size <= 262144 and sha(target.read_bytes()) == self.cli_pin, 'CLI_ENTRY_PIN_MISMATCH')
                files.admit(self.records, {'pause_paths': self.pause_paths, 'schedule': self.schedule}, self.policy, self.uid)
                key = tuple(row[k] for k in ('project_id', 'batch_id', 'task_id'))
                claim = journal.claim_intent(self.connection, key, pin, 0)
                claimed = self._confirmed(claim, {'CLAIM_RECORDED'})
                need(claim['report']['new_claim'] is True and claimed['state'] == 'SUBMISSION_UNKNOWN'
                     and claimed['revision'] == 1, 'CLAIM_UNCONFIRMED')
                try:
                    observed = runner(request['argv'])
                    need(type(observed) is dict and set(observed) == {'reason', 'returncode', 'stdout', 'stderr_sha256', 'stderr_bytes', 'streams_complete'}
                         and observed['reason'] in {'SPAWN_FAILED', 'CLI_RETURNED', 'CLI_TIMEOUT_UNKNOWN', 'CLI_STREAM_LIMIT_UNKNOWN', 'CLI_IO_UNKNOWN'}
                         and (observed['returncode'] is None or type(observed['returncode']) is int)
                         and type(observed['stdout']) is bytes and len(observed['stdout']) <= MAX_STREAM
                         and journal._hex(observed['stderr_sha256']) and type(observed['stderr_bytes']) is int
                         and 0 <= observed['stderr_bytes'] <= MAX_STREAM and type(observed['streams_complete']) is bool, 'CLI_RESULT_INVALID')
                except Exception:
                    observed = {'reason': 'CLI_IO_UNKNOWN', 'returncode': None, 'stdout': b'',
                                'stderr_sha256': sha(b''), 'stderr_bytes': 0, 'streams_complete': False}
                observed = dict(observed)
                stdout = observed.pop('stdout')
                receipt = dict(observed, schema=1, request_sha256=request_pin, stdout_sha256=sha(stdout),
                               stdout_bytes=len(stdout), native_ack_verified=False, replay_permitted=False)
                write_once(folder / 'stdout.bin', stdout, self.uid)
                write_once(folder / 'cli-result.json', canonical(receipt), self.uid)
                return self.read(pin, request_pin)
        except files.PackageError as error:
            return {'status': 'UNCONFIRMED', 'reason': str(error), 'replay_permitted': False, 'native_ready': False}
        except Exception:
            return {'status': 'UNCONFIRMED', 'reason': 'CONTROL_UNCONFIRMED', 'replay_permitted': False, 'native_ready': False}

    def _request(self, pin, request_pin):
        need(journal._hex(pin) and journal._hex(request_pin), 'CONTROL_PIN_INVALID')
        folder = self.records / pin; raw = durable_read(folder / 'request.json', self.uid)
        need(sha(raw) == request_pin, 'REQUEST_PIN_MISMATCH')
        request = files.decode(raw)
        need(request['intent_sha256'] == pin and request['role'] in self.roles
             and request['thread_id'] == self.roles[request['role']]
             and request['settings_sha256'] == sha(canonical(self.settings())), 'CONTROL_BINDING_MISMATCH')
        key = files.decode(durable_read(folder / 'intent-key.json', self.uid))
        need(type(key) is list and journal._key(tuple(key)), 'INTENT_KEY_INVALID')
        row = self._confirmed(journal.read_intent(self.connection, tuple(key), pin), {'INTENT_OBSERVED'})
        value = files.decode(row['intent_bytes'])
        need(value['settings_sha256'] == request['settings_sha256']
             and value['prompt_sha256'] == sha(request['prompt'].encode('utf8')), 'CONTROL_BINDING_MISMATCH')
        return folder, request, row

    def read(self, pin, request_pin):
        folder, _request, row = self._request(pin, request_pin)
        path = folder / 'cli-result.json'
        receipt_raw = durable_read(path, self.uid) if path.exists() else None
        receipt = files.decode(receipt_raw) if receipt_raw is not None else None
        receipt_pin = sha(receipt_raw) if receipt_raw is not None else None
        if receipt is not None:
            need(row['state'] == 'SUBMISSION_UNKNOWN' and canonical(receipt) == receipt_raw
                 and receipt['reason'] in {'SPAWN_FAILED','CLI_RETURNED','CLI_TIMEOUT_UNKNOWN','CLI_STREAM_LIMIT_UNKNOWN','CLI_IO_UNKNOWN'}
                 and receipt['request_sha256'] == request_pin and receipt['native_ack_verified'] is False
                 and receipt['stdout_sha256'] == sha(durable_read(folder / 'stdout.bin', self.uid)), 'CLI_RECEIPT_MISMATCH')
        return {'status': row['state'], 'reason': 'CLI_OBSERVED_UNMAPPED' if receipt else 'CLI_RESULT_ABSENT',
                'request_sha256': request_pin, 'cli_receipt_sha256': receipt_pin,
                'cli_returncode': receipt['returncode'] if receipt else None,
                'cli_reason': receipt['reason'] if receipt else None,
                'native_ack_verified': False, 'replay_permitted': False, 'native_ready': False}

    def observe(self, pin, request_pin, binding, owner_client):
        with files.lock(self.records, self.uid):
            folder, request, row = self._request(pin, request_pin)
            need(row['state'] == 'SUBMISSION_UNKNOWN' and row['revision'] == 1, 'INTENT_NOT_SUBMITTED')
            if binding is None:
                return {'status': 'UNBOUND', 'reason': 'OWNER_TURN_BINDING_REQUIRED', 'native_ready': False}
            need(type(binding) is dict and set(binding) == {'schema', 'role', 'thread_id', 'turn_id', 'user_message_item_id',
                 'observed_client_id', 'prompt_sha256', 'request_sha256', 'cli_receipt_sha256'}
                 and type(binding['schema']) is int and binding['schema'] == 1
                 and binding['role'] == request['role'] and binding['thread_id'] == request['thread_id']
                 and binding['request_sha256'] == request_pin
                 and binding['prompt_sha256'] == sha(request['prompt'].encode('utf8'))
                 and all(type(binding[x]) is str and journal._identifier(binding[x]) for x in ('turn_id','user_message_item_id'))
                 and (binding['observed_client_id'] is None or (type(binding['observed_client_id']) is str
                      and journal._identifier(binding['observed_client_id']))), 'OWNER_BINDING_INVALID')
            local = self.read(pin, request_pin)
            need(binding['cli_receipt_sha256'] == local['cli_receipt_sha256'], 'OWNER_RECEIPT_BINDING_MISMATCH')
            write_once(folder / 'owner-binding.json', canonical(binding), self.uid)
            terminal = folder / 'terminal.json'
            if terminal.exists():
                return files.decode(durable_read(terminal, self.uid))
            result = observe_bound_turn(owner_client, binding, request['prompt'])
            result.update(request_sha256=request_pin, owner_binding_sha256=sha(canonical(binding)))
            data = canonical(result)
            write_once(folder / ('observation-' + sha(data) + '.json'), data, self.uid)
            if result['status'] in ('TURN_COMPLETED', 'TURN_FAILED', 'TURN_INTERRUPTED'):
                write_once(terminal, data, self.uid)
            return result

    def record_delivery(self, pin, request_pin, expected_binding, expected_apps, evidence, draft_pr):
        with files.lock(self.records, self.uid):
            folder, _request, row = self._request(pin, request_pin)
            terminal = files.decode(durable_read(folder / 'terminal.json', self.uid))
            need(terminal['status'] == 'TURN_COMPLETED' and terminal['request_sha256'] == request_pin, 'TURN_NOT_COMPLETED')
            intent = files.decode(row['intent_bytes'])
            need(type(expected_binding) is dict and expected_binding.get('repository') == intent['repository']
                 and expected_binding.get('base_sha') == intent['base_sha']
                 and expected_binding.get('plan_sha256') == intent['plan_sha256'], 'DELIVERY_INTENT_BINDING_MISMATCH')
            compared = audit.audit_bound_checks(expected_binding, expected_apps, evidence)
            draft_shape = (type(draft_pr) is dict and all(type(draft_pr.get(x)) is dict
                           and type(draft_pr[x].get('repo')) is dict for x in ('base','head')))
            draft_match = (draft_shape and draft_pr.get('draft') is True and draft_pr.get('state') == 'open'
                           and type(draft_pr.get('number')) is int and draft_pr.get('number') == expected_binding.get('pr_number')
                           and draft_pr.get('base', {}).get('sha') == expected_binding.get('base_sha')
                           and draft_pr.get('head', {}).get('sha') == expected_binding.get('head_sha')
                           and draft_pr.get('base', {}).get('repo', {}).get('full_name') == expected_binding.get('repository')
                           and draft_pr.get('head', {}).get('repo', {}).get('full_name') == expected_binding.get('repository'))
            result = {'status': 'DELIVERY_EVIDENCE_RECORDED' if compared['structural_satisfied'] and draft_match else 'DELIVERY_EVIDENCE_PENDING',
                      'check_comparison': compared, 'draft_pr_fields_match': draft_match,
                      'request_sha256': request_pin, 'evidence_sha256': sha(canonical(evidence)),
                      'draft_snapshot_sha256': sha(canonical(draft_pr)), 'input_origin_verified': False,
                      'independent_review_verified': False, 'native_ready': False, 'merge_authorized': False}
            data = canonical(result); write_once(folder / ('delivery-' + sha(data) + '.json'), data, self.uid)
            return result
