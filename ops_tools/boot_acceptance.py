#!/usr/bin/env python3
"""Native isolated unit/Docker boot-policy transition; never reboot the host."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops_platform.contracts import ReadFailure, require
from ops_platform.ops import docker_acceptance as isolated, service_acceptance as native
from ops_platform.ops import manage, worker_state as ws, refresh_worker as worker
from ops_platform.ops import service_lifecycle as service, restart_check as rc, lifecycle_state as local
from ops_platform.ops import refresh_discovery as refresh, set_public_url as change
from ops_platform.ops.watch_acceptance import snapshot
from ops_platform.transport import strict_json

STAGE = 'O04-isolated-native-boot-transition'
KIND = 'isolated-native-boot-transition'
TRANSITION = Path('ops_candidates/boot_transition_20260929')
START = Path('ops_candidates/boot_start_20260929')
ENTRY = Path('ops_tools/boot_acceptance.py')
BASE_HASH = 'a24257d8636b5842167e74f7b02931c467d105519fb9b5f70ceb577437fccca4'
CANDIDATE_HASH = '2bf14042a6aa15e4e12b46aa7be494eef2b8834c9b44c4a5587610d5d3a3bb1f'
TARGETS = {'ops_platform/ops/' + name for name in
           ('worker_state.py', 'boot_lifecycle.py', 'refresh_worker.py', 'service_lifecycle.py', 'service_entry.py')}
LIFECYCLE_FD = None


def lifecycle_lock(directory):
    return ws.file_lock(directory / 'boot-lifecycle.lock', 'BOOT_ACCEPTANCE_RUN_STILL_ACTIVE')


def inherited_lease(directory, fd):
    require(type(fd) is int and fd >= 3, 'BOOT_ACCEPTANCE_LEASE_REQUIRED')
    path = manage.safe(directory / 'boot-lifecycle.lock')
    a, b = os.fstat(fd), path.stat()
    require(stat.S_ISREG(a.st_mode) and a.st_uid == os.getuid() and not a.st_mode & 0o077
            and a.st_nlink == 1 and (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino),
            'BOOT_ACCEPTANCE_LEASE_INVALID')
    # A separate open must be blocked by the actual inherited flock. No saved
    # PID, path alone, or descriptor number is accepted as liveness evidence.
    try:
        with lifecycle_lock(directory): pass
    except ReadFailure as exc:
        require(exc.code == 'BOOT_ACCEPTANCE_RUN_STILL_ACTIVE', exc.code)
        return fd
    raise ReadFailure('BOOT_ACCEPTANCE_LEASE_NOT_HELD')


def digest(raw): return hashlib.sha256(raw).hexdigest()


def error_report(exc):
    report = native.error_report(exc)
    code = getattr(exc, 'duplicate_start_rejection', None)
    if isinstance(code, str):
        report['duplicate_start_rejection'] = code if re.fullmatch('[A-Z][A-Z0-9_]{0,95}', code) else 'UNCLASSIFIED_GUARD_RESPONSE'
    return report


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value); return value


def read(path, limit=1024*1024):
    raw, _ = rc.read_file(manage.safe(path), limit, private=True)
    require(path.stat().st_nlink == 1, 'BOOT_ACCEPTANCE_HARDLINK_REFUSED')
    return raw


def candidate_sources(root):
    """Read only fixed source paths, never state/keys or arbitrary manifest paths."""
    paths = [ENTRY, TRANSITION / 'manifest.json', TRANSITION / 'transition.py',
             START / 'manifest.json', START / 'arming.py']
    paths += [TRANSITION / 'source' / rel for rel in sorted(TARGETS)]
    raw = {}
    for rel in paths:
        p = manage.safe(root / rel)
        require(p.is_file() and p.stat().st_nlink == 1 and p.stat().st_size <= 1024*1024,
                'BOOT_ACCEPTANCE_SOURCE_INVALID')
        raw[str(rel)] = p.read_bytes()
    spec = strict_json(raw[str(TRANSITION / 'manifest.json')])
    arming = strict_json(raw[str(START / 'manifest.json')])
    require(spec.get('base_code_sha256') == BASE_HASH and spec.get('candidate_code_sha256') == CANDIDATE_HASH
            and spec.get('activation_supported') is False, 'BOOT_ACCEPTANCE_CANDIDATE_CHANGED')
    require(len(spec['changes']) == 5 and {row['path'] for row in spec['changes']} == TARGETS,
            'BOOT_ACCEPTANCE_SOURCE_SCOPE')
    for row in spec['changes']:
        require(digest(raw[str(TRANSITION / 'source' / row['path'])]) == row['after_sha256'],
                'BOOT_ACCEPTANCE_PAYLOAD_CHANGED')
    expected = {row['path']: row['sha256'] for row in spec['auxiliary_files']}
    require(digest(raw[str(TRANSITION / 'transition.py')]) == expected['transition.py'], 'BOOT_ACCEPTANCE_PAYLOAD_CHANGED')
    require(arming.get('activation_supported') is False and arming.get('production_cli_available') is False
            and arming['transition_manifest_sha256'] == digest(raw[str(TRANSITION / 'manifest.json')])
            and arming['base_code_sha256'] == BASE_HASH and arming['candidate_code_sha256'] == CANDIDATE_HASH,
            'BOOT_ACCEPTANCE_ARMING_CHANGED')
    expected = {row['path']: row['sha256'] for row in arming['auxiliary_files']}
    require(digest(raw[str(START / 'arming.py')]) == expected['arming.py'], 'BOOT_ACCEPTANCE_ARMING_CHANGED')
    return raw, spec


def preflight():
    report = native.native_preflight()
    require(report['linger'] is True, 'BOOT_ACCEPTANCE_LINGER_REQUIRED')
    candidate_sources(ROOT)
    require(worker.code_hash(ROOT) == BASE_HASH, 'BOOT_ACCEPTANCE_BASE_CHANGED')
    return dict(report, stage=STAGE, result='passed', scope='read_only_user_manager_and_source_metadata',
                persistence='boot', required_persistence='boot', files_written=[], linger_changed=False,
                host_reboot_requested=False, production_service_control_requested=False)


def create_run(base, engine):
    # The shared service scenario deliberately makes its old generic cleanup
    # refuse transitioned source. This entry handles reviewed before/after code.
    raw, spec = candidate_sources(ROOT)
    require(worker.code_hash(ROOT) == BASE_HASH, 'BOOT_ACCEPTANCE_BASE_CHANGED')
    directory, record = isolated.create_run(base, engine, scenario='service')
    project = directory / 'project'
    for rel, body in raw.items():
        p = project / rel; p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        p.write_bytes(body); p.chmod(0o600)
    marker = {'schema': 1, 'kind': KIND, 'directory': str(directory), 'owner': record['owner'],
              'base_code_sha256': BASE_HASH, 'candidate_code_sha256': CANDIDATE_HASH,
              'source_base_sha256': change.digest(record['source_hashes']),
              'auxiliary_hashes': {rel: digest(body) for rel, body in raw.items()}}
    isolated.write(directory / 'boot-run.json', marker)
    validate(directory)
    return directory, record


def validate(directory):
    record = isolated.validate_run(directory)
    require(record.get('scenario') == 'service', 'BOOT_ACCEPTANCE_SCENARIO_INVALID')
    marker = strict_json(read(directory / 'boot-run.json', 128*1024))
    require(marker.get('schema') == 1 and marker.get('kind') == KIND
            and marker.get('directory') == str(directory) and marker.get('owner') == record['owner']
            and marker.get('base_code_sha256') == BASE_HASH and marker.get('candidate_code_sha256') == CANDIDATE_HASH
            and marker.get('source_base_sha256') == change.digest(record['source_hashes']), 'BOOT_ACCEPTANCE_RECORD_INVALID')
    expected, spec = candidate_sources(ROOT)
    require(marker.get('auxiliary_hashes') == {rel: digest(raw) for rel, raw in expected.items()},
            'BOOT_ACCEPTANCE_TOOL_CHANGED')
    project = directory / 'project'
    for rel, wanted in marker['auxiliary_hashes'].items():
        require(digest(read(project / rel)) == wanted, 'BOOT_ACCEPTANCE_TOOL_CHANGED')
    runtime = {row['path']: row['sha256'] for row in spec['base_files']}
    require(len(runtime) == 60 and all(record['source_hashes'].get(k) == v for k, v in runtime.items()),
            'BOOT_ACCEPTANCE_BASE_CHANGED')
    changed = {row['path']: row for row in spec['changes']}
    for rel, wanted in record['source_hashes'].items():
        p = Path(rel)
        require(not p.is_absolute() and '..' not in p.parts, 'BOOT_ACCEPTANCE_SOURCE_SCOPE')
        allowed = {wanted}
        if rel in changed: allowed.add(changed[rel]['after_sha256'])
        require(digest(read(project / p)) in allowed, 'BOOT_ACCEPTANCE_SOURCE_CHANGED')
    extra = project / 'ops_platform/ops/boot_lifecycle.py'
    if extra.exists() or extra.is_symlink():
        require(digest(read(extra)) == changed['ops_platform/ops/boot_lifecycle.py']['after_sha256'], 'BOOT_ACCEPTANCE_SOURCE_CHANGED')
    actual = {str(p.relative_to(project)) for folder in ('ops_platform', 'infra/observability/ops', 'infra/langfuse/ops')
              for p in (project / folder).rglob('*.py') if 'tests' not in p.relative_to(project).parts}
    require(actual in (set(runtime), set(runtime) | {'ops_platform/ops/boot_lifecycle.py'}), 'BOOT_ACCEPTANCE_SOURCE_SET_CHANGED')
    return record


def checked_state(directory):
    validate(directory)
    require(ROOT == directory / 'project', 'BOOT_ACCEPTANCE_COPIED_ENTRY_REQUIRED')
    return native.checked_state(directory)[0]


def child_env(record):
    env = {k: v for k, v in os.environ.items() if not k.startswith(('OPS_', 'COMPOSE_', 'PYTHON'))
           and k not in ('DOCKER_CONTEXT', 'DOCKER_API_VERSION', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')}
    env.update(DOCKER_HOST=record['endpoint'], PYTHONDONTWRITEBYTECODE='1')
    return env


def invoke(directory, action, timeout=1000):
    require(action in ('worker', 'candidate', 'restored', 'cleanup-unit'), 'BOOT_ACCEPTANCE_ACTION_INVALID')
    record = validate(directory); project = directory / 'project'
    args = [sys.executable, '-I', '-B', str(project / ENTRY), action, '--run-dir', str(directory)]
    inherited = ()
    if action in ('candidate', 'restored'):
        inherited = (inherited_lease(directory, LIFECYCLE_FD),)
        args += ['--lease-fd', str(LIFECYCLE_FD)]
    path = directory / (action + '.log')
    if path.exists(): manage.private(path)
    with path.open('w') as stream:
        path.chmod(0o600)
        child = subprocess.Popen(args, cwd=project, env=child_env(record), stdin=subprocess.DEVNULL,
            stdout=stream, start_new_session=True, pass_fds=inherited)
        try: code = child.wait(timeout=timeout)
        except BaseException:
            isolated.stop_worker(child); raise
    require(path.stat().st_size < 128*1024, 'BOOT_ACCEPTANCE_REPORT_LIMIT')
    answer = strict_json(read(path, 128*1024))
    if code != 0 or answer.get('result') != 'passed':
        exc = ReadFailure(answer.get('code', 'BOOT_ACCEPTANCE_CHILD_FAILED'))
        exc.service_action = answer.get('failure_context', {}).get('action')
        exc.service_command = answer.get('failure_context', {}).get('service_command')
        exc.duplicate_start_rejection = answer.get('duplicate_start_rejection')
        raise exc
    require(answer.get('stage') == STAGE and answer.get('action') == action, 'BOOT_ACCEPTANCE_REPORT_MISMATCH')
    return answer


def cleanup_unit(directory):
    validate(directory)
    require(ROOT == directory / 'project', 'BOOT_ACCEPTANCE_COPIED_ENTRY_REQUIRED')
    # Descendants inherit the outer orchestration lease. Even loss of their
    # supervisor cannot make cleanup race an unfinished start/rollback helper.
    with lifecycle_lock(directory):
        report = native.cleanup_unit(directory)
    require(report.get('result') == 'passed' and report.get('worker_exit_verified') is True,
            'BOOT_ACCEPTANCE_UNIT_CLEANUP_UNCERTAIN')
    return dict(report, stage=STAGE, action='cleanup-unit')


class Engine(isolated.Engine):
    def cleanup(self, directory):
        record = validate(directory)
        # A user service outlives the runner. Verify exit/absence before any
        # container removal, including timeout/error and explicit cleanup paths.
        unit = invoke(directory, 'cleanup-unit', timeout=280)
        owned = self.owned(directory, record)
        for ident in owned:
            require(ident in self.owned(directory, record), 'ACCEPTANCE_CONTAINER_OWNERSHIP_CHANGED')
            self.command(['container', 'rm', '-f', ident], timeout=30)
        networks = self.command(['network', 'ls', '--no-trunc', '-q', '--filter',
                                 'label=' + isolated.LABEL + '=' + record['owner']]).split()
        require(len(networks) <= 8 and all(re.fullmatch('[a-f0-9]{64}', x) for x in networks), 'ACCEPTANCE_NETWORK_LIMIT')
        for ident in networks:
            fmt = '{"owner":{{json (index .Labels "' + isolated.LABEL + '")}},"containers":{{json .Containers}}}'
            net = strict_json(self.command(['network', 'inspect', '--format', fmt, ident]))
            require(net['owner'] == record['owner'] and not net['containers'], 'ACCEPTANCE_NETWORK_IN_USE')
            self.command(['network', 'rm', ident])
        require(not self.owned(directory, record), 'ACCEPTANCE_CLEANUP_INCOMPLETE')
        self.protect(record['protected'])
        return {'result': 'passed', 'service': unit, 'owned_containers_removed': len(owned),
                'owned_networks_removed': len(networks), 'protected_containers_unchanged': len(record['protected']),
                'images_or_volumes_removed': False}


def phase(directory, engine, text):
    engine.protect(validate(directory)['protected'])
    print('[boot-acceptance] ' + text, file=sys.stderr, flush=True)


def worker_phase(directory, engine):
    validate(directory); require(ROOT == directory / 'project', 'BOOT_ACCEPTANCE_COPIED_ENTRY_REQUIRED')
    phase(directory, engine, 'prepare isolated Ops and start baseline session unit')
    c = isolated.prepare_test_ops(directory, engine)
    state = checked_state(directory); ctl = native.Controller(state)
    ctl.good('install'); ctl.good('start'); first = ctl.wait(native.healthy)
    require(not native.observation(first)['outcome']['restart_requested'], 'BOOT_ACCEPTANCE_BASELINE_CHANGED')
    baseline = snapshot(c)
    phase(directory, engine, 'stop baseline unit, transition code and policy, remain paused')
    tx = module('boot_transition_candidate', ROOT / TRANSITION / 'transition.py')
    transaction = tx.Transition(state, tx.Bundle(ROOT / TRANSITION), ctl.backend)
    unit_before = Path(service.load(state)['unit_path']).read_bytes()
    outcome = transaction.apply(transaction.plan('boot'))
    require(outcome['phase'] == 'committed' and outcome['desired_state'] == 'paused'
            and not service.enabled(service.load(state)), 'BOOT_ACCEPTANCE_TRANSITION_FAILED')
    require(Path(service.load(state)['unit_path']).read_bytes() == unit_before
            and snapshot(c) == baseline, 'BOOT_ACCEPTANCE_TRANSITION_CHANGED_OPS')
    # Fresh process is essential: the preceding interpreter still contains
    # baseline modules even though the source and two bindings were migrated.
    candidate = invoke(directory, 'candidate', timeout=700)
    require(worker.code_hash(ROOT) == BASE_HASH and snapshot(c) == baseline, 'BOOT_ACCEPTANCE_ROLLBACK_FAILED')
    restored = invoke(directory, 'restored', timeout=300)
    isolated.preserve_test_ops(c, engine)
    require(snapshot(c) == baseline, 'BOOT_ACCEPTANCE_RESTORED_OPS_CHANGED')
    phase(directory, engine, 'completed; parent rechecks unit absence before Docker cleanup')
    cases = {'baseline_session_new_evaluation': 'passed', 'native_stop_code_policy_transition': 'passed',
             'same_unit_bytes_and_test_ops_preserved': 'passed', **candidate['cases'], **restored['cases']}
    return {'schema': 1, 'stage': STAGE, 'action': 'worker', 'result': 'passed', 'checked_at': rc.now(),
            'scope': 'native_user_unit_docker_same_boot_with_synthetic_upstreams', 'cases': cases,
            'image_id': c['record']['image'], 'runtime_source_hashes_verified': c['hashes'],
            'manager_version': ctl.manager_version, 'test_unit_name': service.load(state)['unit_name'],
            'base_code_sha256': BASE_HASH, 'candidate_code_sha256': CANDIDATE_HASH,
            'source_restored_to_base': True, 'test_unit_removed': True, 'test_worker_exit_verified': True,
            'same_test_ops_container_image_mounts': True, 'test_ops_restart_requested': False,
            'synthetic_credentials_preserved': True, 'initial_cache_entries_preserved': len(c['initial_cache']['success_times']),
            'bootstrap_record_preserved': True, 'refresh_journal_count': len(refresh.lineage(state)['records']),
            'duplicate_start_guard': candidate['duplicate_start_guard'],
            'production_worker_started': False, 'production_service_control_requested': False,
            'production_credentials_read': False, 'production_application_api_requests': 0,
            'inference_requested': False, 'image_build_or_pull_requested': False,
            'linger_changed': False, 'existing_linger_required': True, 'autostart_enabled_after_cleanup': False,
            'host_reboot_requested': False, 'logout_or_boot_acceptance': False, 'long_running_acceptance': False,
            'promql_engine_acceptance': False, 'kubernetes_acceptance': 'deferred'}


def duplicate_start_guard(start, proposal, ctl):
    """A live worker may reject on its singleton lease before journal lookup.

    Accept only the two specified refusal paths, with the accepted journal,
    receipt, control, bindings, unit and live invocation still unchanged.
    """
    state = start.directory
    store = ws.Store(state)
    manifest = service.load(state)
    accepted = start.load()
    before = ctl.status()
    require(accepted['phase'] == 'accepted' and before['persistence'] == 'boot'
            and before['enabled'] and before['desired_state'] == 'running'
            and before['recovery'] is None and native.healthy(before)
            and accepted['accepted_service_invocation'] == native.invocation(before),
            'BOOT_ACCEPTANCE_DUPLICATE_PRECONDITION_FAILED')
    paths = (start.path, start.transition.path, service.manifest_path(state),
             store.path / 'owner.json', store.path / 'control.json', store.path / 'boot.json',
             Path(manifest['unit_path']))
    def preserved():
        require(service.enabled(manifest), 'BOOT_ACCEPTANCE_ENABLE_LINK_CHANGED')
        return {str(p): read(p) for p in paths}
    checkpoint = preserved()
    try:
        start.apply(proposal)
    except ReadFailure as exc:
        rejection = exc.code
        if rejection not in ('BOOT_START_ALREADY_RECORDED', 'AUTOMATIC_WORKER_BUSY'):
            failure = ReadFailure('BOOT_ACCEPTANCE_DUPLICATE_GUARD_FAILED')
            failure.duplicate_start_rejection = rejection
            raise failure from None
    else:
        raise ReadFailure('BOOT_ACCEPTANCE_DUPLICATE_START_ALLOWED')
    after = ctl.status()
    require(preserved() == checkpoint, 'BOOT_ACCEPTANCE_DUPLICATE_MUTATED_STATE')
    require(native.invocation(after) == native.invocation(before)
            and after['provider_status']['MainPID'] == before['provider_status']['MainPID']
            and native.observation(after).get('identity') == native.observation(before).get('identity')
            and after['desired_state'] == 'running' and after['recovery'] is None
            and after['persistence'] == 'boot' and after['enabled'] and native.healthy(after),
            'BOOT_ACCEPTANCE_DUPLICATE_CHANGED_WORKER')
    return {'rejection_code': rejection, 'accepted_records_control_bindings_and_unit_preserved': True,
            'same_manager_invocation_and_worker': True, 'enabled_link_preserved': True}


def candidate_phase(directory, engine):
    state = checked_state(directory); ctl = native.Controller(state)
    require(worker.code_hash(ROOT) == CANDIDATE_HASH, 'BOOT_ACCEPTANCE_TARGET_CODE_REQUIRED')
    tx = module('boot_transition_candidate', ROOT / TRANSITION / 'transition.py')
    arming = module('boot_start_candidate', ROOT / START / 'arming.py')
    transaction = tx.Transition(state, tx.Bundle(ROOT / TRANSITION), ctl.backend)
    start = arming.Start(transaction); control = directory / 'control/control.json'; cases = {}
    phase(directory, engine, 'readback failure preserves paused state; restore synthetic source')
    proposal = start.plan()
    before = ws.Store(state).control()
    isolated.write(control, {'version': 0, 'rules_fault': True})
    try:
        try:
            start.apply(proposal); raise ReadFailure('BOOT_ACCEPTANCE_EXPECTED_READBACK_FAILURE')
        except ReadFailure as exc: require(exc.code == 'OPS_ALERT_READBACK_FAILED', 'BOOT_ACCEPTANCE_WRONG_READBACK_FAILURE')
        current = service.owned(service.load(state), ctl.backend)
        require(service.inactive(current) and ctl.backend.cgroup_empty(current)
                and ws.Store(state).control() == before and not start.path.exists()
                and not service.enabled(service.load(state)), 'BOOT_ACCEPTANCE_READBACK_FAILURE_MUTATED_STATE')
    finally: isolated.write(control, {'version': 0})
    cases['native_readback_failure_keeps_paused_disabled'] = 'passed'
    phase(directory, engine, 'arm once, enable owned test unit and verify fresh boot-policy evaluation')
    result = start.apply(start.plan())
    require(result['result'] == 'passed' and result.get('fresh_evaluation_verified') is True,
            result.get('code', 'BOOT_ACCEPTANCE_START_FAILED'))
    first = ctl.status(); invocation = native.invocation(first)
    require(first['persistence'] == 'boot' and first['enabled'] and native.healthy(first), 'BOOT_ACCEPTANCE_BOOT_POLICY_NOT_RUNNING')
    cases['native_armed_receipt_new_invocation_and_evaluation'] = 'passed'
    duplicate = duplicate_start_guard(start, proposal, ctl)
    require(native.invocation(ctl.status()) == invocation, 'BOOT_ACCEPTANCE_INVOCATION_REPLACED')
    cases['duplicate_start_refused_without_replacement'] = 'passed'
    phase(directory, engine, 'unchanged next evaluation, explicit cancel and native code rollback')
    follow = ctl.wait(lambda value: native.healthy(value, native.observation(first)['completed_cycles'] + 1))
    require(native.invocation(follow) == invocation and not native.observation(follow)['outcome']['restart_requested'],
            'BOOT_ACCEPTANCE_FOLLOWUP_CHANGED')
    cases['unchanged_boot_policy_followup'] = 'passed'
    cancelled = start.cancel()
    require(cancelled['worker_exit_verified'] and not service.enabled(service.load(state)), 'BOOT_ACCEPTANCE_CANCEL_FAILED')
    cases['native_cancel_exit_lease_and_disable'] = 'passed'
    rollback = transaction.rollback()
    require(rollback['phase'] == 'rolled_back' and worker.code_hash(ROOT) == BASE_HASH
            and service.load(state)['persistence'] == 'session', 'BOOT_ACCEPTANCE_ROLLBACK_FAILED')
    cases['native_source_policy_and_dual_binding_rollback'] = 'passed'
    return {'stage': STAGE, 'action': 'candidate', 'result': 'passed', 'cases': cases,
            'duplicate_start_guard': duplicate}


def restored_phase(directory, engine):
    state = checked_state(directory); ctl = native.Controller(state)
    require(worker.code_hash(ROOT) == BASE_HASH, 'BOOT_ACCEPTANCE_BASE_CHANGED')
    phase(directory, engine, 'fresh baseline interpreter, explicit session resume and unit removal')
    previous = native.invocation(ctl.status())
    ctl.good('resume'); value = ctl.wait(native.healthy)
    require(native.invocation(value) != previous and value['persistence'] == 'session' and not value['enabled']
            and not native.observation(value)['outcome']['restart_requested'], 'BOOT_ACCEPTANCE_SESSION_RESTORE_FAILED')
    ctl.good('stop'); ctl.good('remove')
    final = ctl.status()
    require(final['status'] == 'removed' and not final['enabled'], 'BOOT_ACCEPTANCE_UNIT_REMOVAL_FAILED')
    return {'stage': STAGE, 'action': 'restored', 'result': 'passed', 'cases': {
        'restored_session_explicit_resume_new_evaluation': 'passed', 'owned_unit_removed_audit_preserved': 'passed'}}


def run(base):
    preflight()  # Before creating any directory, test unit or container.
    with isolated.run_lock(base):
        engine = Engine(); directory, record = create_run(base, engine)
        print('[boot-acceptance] evidence: ' + str(directory), file=sys.stderr, flush=True)
        result = {'schema': 1, 'stage': STAGE, 'result': 'attention_required'}
        try:
            for role, (name, subtree) in isolated.ROLES.items():
                rel = str(Path(subtree) / ('acceptance-' + role + '.json'))
                engine.compose(directory, rel, name)
                if role == 'model': engine.compose(directory, rel, 'manual-model')
            result = invoke(directory, 'worker', timeout=1100)
        except (KeyboardInterrupt, subprocess.TimeoutExpired) as exc:
            result['code'] = 'BOOT_ACCEPTANCE_INTERRUPTED' if isinstance(exc, KeyboardInterrupt) else 'BOOT_ACCEPTANCE_DEADLINE'
        except local.ERRORS as exc:
            result.update(error_report(exc), stage=STAGE)
        finally:
            try: cleanup = engine.cleanup(directory)
            except (subprocess.TimeoutExpired, *local.ERRORS) as exc:
                cleanup = {'result': 'attention_required', 'code': local.error_code(exc)}
                result['result'] = 'attention_required'
            result.update(run_directory=str(directory), cleanup=cleanup)
            isolated.write(directory / 'boot-report.json', result)
        return result


def main():
    global LIFECYCLE_FD
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'run', 'cleanup', 'worker', 'candidate', 'restored', 'cleanup-unit'))
    parser.add_argument('--state-dir', type=Path, default=isolated.BASE)
    parser.add_argument('--run-dir', type=Path)
    parser.add_argument('--lease-fd', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        require(args.lease_fd is None or args.action in ('candidate', 'restored'), 'BOOT_ACCEPTANCE_ACTION_INVALID')
        if args.action in ('preflight', 'run'):
            require(args.run_dir is None, 'ACCEPTANCE_RUN_DIR_NOT_ALLOWED')
            report = preflight() if args.action == 'preflight' else run(args.state_dir)
        else:
            require(args.run_dir is not None, 'ACCEPTANCE_RUN_DIR_REQUIRED')
            directory = manage.safe(args.run_dir); record = validate(directory)
            if args.action == 'cleanup-unit': report = cleanup_unit(directory)
            else:
                if args.action in ('candidate', 'restored'):
                    LIFECYCLE_FD = inherited_lease(directory, args.lease_fd)
                engine = Engine(record['endpoint'])
                if args.action == 'cleanup':
                    with isolated.run_lock(directory.parent):
                        report = {'stage': STAGE, 'result': 'passed', 'cleanup': engine.cleanup(directory), 'run_directory': str(directory)}
                elif args.action == 'worker':
                    with lifecycle_lock(directory) as fd:
                        LIFECYCLE_FD = fd
                        try: report = worker_phase(directory, engine)
                        finally: LIFECYCLE_FD = None
                else:
                    report = globals()[args.action + '_phase'](directory, engine)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report['result'] == 'passed' else 2
    except (subprocess.TimeoutExpired, *local.ERRORS) as exc:
        print(json.dumps(dict(error_report(exc), schema=1, stage=STAGE, action=args.action,
                              host_reboot_requested=False, production_service_control_requested=False,
                              linger_changed=False)))
        return 2


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    raise SystemExit(main())
