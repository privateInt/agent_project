#!/usr/bin/env python3
"""Read-only backup execution prerequisites. No capture, credentials, service control or writes."""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('_readiness_capacity', ROOT / 'src/ops/tools/plan_backup_capacity.py')
cap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cap)
bp, require = cap.bp, cap.require
STAGE = 'O04-backup-execution-readiness'
RUNTIME = ('src/ops/platform', 'src/infra/observability/ops', 'src/infra/langfuse/ops')
# Reviewed implementations, not host/path defaults. Unknown source requires a new review.
REVIEWED = {'a24257d8636b5842167e74f7b02931c467d105519fb9b5f70ceb577437fccca4',
            '2bf14042a6aa15e4e12b46aa7be494eef2b8834c9b44c4a5587610d5d3a3bb1f'}
GAPS = ('BOOT_MAINTENANCE_RETURN_NOT_IMPLEMENTED', 'STOPPED_STORAGE_HELPER_NOT_IMPLEMENTED',
        'ENCRYPTED_CAPTURE_NOT_IMPLEMENTED', 'ISOLATED_RESTORE_NOT_IMPLEMENTED')
RESOURCE_FORMAT = '[' + ','.join('{{json ' + x + '}}' for x in (
    '.Id', '.Image', '.HostConfig.Memory', '.HostConfig.NanoCpus', '.HostConfig.CpuQuota',
    '.HostConfig.CpuPeriod')) + ']'
CHARSET = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'


def signature(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def code_hash(root):
    paths = sorted(p for folder in RUNTIME for p in (root / folder).rglob('*.py')
                   if 'tests' not in p.relative_to(root).parts)
    require(0 < len(paths) <= 512, 'RUNTIME_SOURCE_SET_INVALID')
    digest = hashlib.sha256()
    for path in paths:
        raw = bp.read_small(path, private=False, json_value=False).encode()
        digest.update(str(path.relative_to(root)).encode() + b'\0' + hashlib.sha256(raw).digest())
    return digest.hexdigest(), len(paths)


def recipient_metadata(values):
    """Only native, lowercase age X25519 Bech32 syntax; never parse identity files."""
    result = {'status': 'not_supplied', 'count': 0, 'fingerprints': [],
              'scope': 'public_recipient_syntax_only', 'identity_file_read': False,
              'key_possession_verified': False, 'encryption_test_executed': False}
    if not values:
        return result
    try:
        require(len(values) <= 8, 'RECIPIENT_COUNT_LIMIT')
        seen = set()
        for value in values:
            require(isinstance(value, str) and len(value) == 62 and value.startswith('age1')
                    and all(c in CHARSET for c in value[4:]), 'INVALID_PUBLIC_RECIPIENT')
            digits = [CHARSET.index(c) for c in value[4:]]
            # Bech32 polynomial over the expanded "age" HRP and payload/checksum.
            checksum = 1
            generators = (0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3)
            for digit in [3, 3, 3, 0, 1, 7, 5, *digits]:
                high = checksum >> 25
                checksum = ((checksum & 0x1ffffff) << 5) ^ digit
                for bit, polynomial in enumerate(generators):
                    if high & (1 << bit): checksum ^= polynomial
            # 52 five-bit groups represent 32 bytes and four zero padding bits.
            require(checksum == 1 and digits[-7] & 15 == 0, 'INVALID_PUBLIC_RECIPIENT')
            require(value not in seen, 'DUPLICATE_PUBLIC_RECIPIENT')
            seen.add(value)
        result.update(status='syntax_valid', count=len(seen),
                      fingerprints=sorted(hashlib.sha256(x.encode()).hexdigest() for x in seen))
    except bp.Unavailable as exc:
        result.update(status='invalid', error=bp.code(exc))
    return result


def inspect_age(which=shutil.which, runner=cap.db.bounded):
    result = {'status': 'not_installed', 'version_commands': 0, 'binary_authenticity_verified': False,
              'backup_compatibility_verified': False, 'key_or_plugin_commands': 0}
    found = which('age')
    if not found:
        return result
    try:
        path = Path(found).resolve(strict=True)
        require(path.is_absolute() and not any(ord(c) < 32 or ord(c) == 127 for c in str(path)), 'AGE_PATH_INVALID')
        info = bp.metadata(path)
        require(info.st_uid in (0, os.getuid()) and not info.st_mode & 0o022
                and not info.st_mode & 0o6000 and os.access(path, os.X_OK)
                and 0 < info.st_size <= 64*1024*1024, 'AGE_BINARY_METADATA_UNSAFE')
        for parent in path.parents:
            st = parent.stat()
            require(st.st_uid in (0, os.getuid()) and (not st.st_mode & 0o022 or st.st_mode & stat.S_ISVTX),
                    'AGE_BINARY_PARENT_UNSAFE')
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for _ in range(65):
                block = stream.read(1024*1024)
                if not block: break
                digest.update(block)
            else: raise bp.Unavailable('AGE_BINARY_SIZE_LIMIT')
        require(signature(bp.metadata(path)) == signature(info), 'AGE_BINARY_CHANGED')
        result['version_commands'] = 1
        rc, out, err = runner([str(path), '--version'], {'PATH': os.defpath, 'LC_ALL': 'C'}, 5)
        require(signature(bp.metadata(path)) == signature(info), 'AGE_BINARY_CHANGED')
        require(rc == 0 and not err and len(out) <= 128, 'AGE_VERSION_QUERY_FAILED')
        version = out.decode('ascii').strip()
        require(re.fullmatch(r'v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]{1,48})?', version),
                'AGE_VERSION_UNRECOGNIZED')
        result.update(status='observed', path=str(path), version=version, sha256=digest.hexdigest())
    except Exception as exc:
        result.update(status='unavailable', error=bp.code(exc))
    return result


class Docker(cap.Docker):
    """Add a fixed resource projection; forbid every inherited container exec path."""
    def execute(self, *args, **kwargs):
        raise bp.Unavailable('READINESS_EXEC_REFUSED')

    def logical_size(self, *args, **kwargs):
        raise bp.Unavailable('READINESS_EXEC_REFUSED')

    def resources(self, row):
        require(cap.db.IDENT.fullmatch(row['id']) and self.base.endpoint, 'RESOURCE_TARGET_INVALID')
        args = ['docker', '--host', self.base.endpoint, 'container', 'inspect', '--format', RESOURCE_FORMAT, row['id']]
        self.calls += 1
        rc, out, _ = self.runner(args, self.base.env, 10)
        require(rc == 0, 'RESOURCE_QUERY_FAILED')
        data = json.loads(out)
        require(isinstance(data, list) and len(data) == 6 and data[:2] == [row['id'], row['image_id']],
                'RESOURCE_IDENTITY_CHANGED')
        memory, nano, quota, period = data[2:]
        require(all(type(v) is int and 0 <= v <= 2**63-1 for v in (memory, nano, period))
                and type(quota) is int and -1 <= quota <= 2**63-1 and not (nano and quota > 0)
                and (quota <= 0 or period > 0), 'RESOURCE_LIMIT_INVALID')
        return {'memory_limit_bytes': memory or None,
                'cpu_limit': nano / 1e9 if nano else quota / period if quota > 0 else None}


def worker_metadata(root, directory):
    owner = bp.read_small(directory / 'owner.json')
    require(owner.get('schema') == 1 and owner.get('uid') == os.getuid() and owner.get('gid') == os.getgid()
            and owner.get('project_root') == str(root) and str(bp.uuid.UUID(owner['owner'])) == owner['owner'],
            'WORKER_INSTALLATION_OWNER_MISMATCH')
    actual, count = code_hash(root)
    reviewed = actual in REVIEWED
    result = {'status': 'not_installed', 'code_sha256': actual, 'code_files': count,
              'source_review': 'reviewed' if reviewed else 'unreviewed',
              'maintenance_return_supported': False if reviewed else None,
              'live_health_checked': False, 'saved_state_is_live_health': False}
    management = directory.parent / 'auto-refresh' / owner['owner']
    if not management.exists() and not management.is_symlink(): return result
    bp.metadata(management, private=True, directory=True)
    service = bp.read_small(management / 'service.json')
    worker = bp.read_small(management / 'worker/owner.json')
    control = bp.read_small(management / 'worker/control.json')
    binding = service.get('binding', {})
    require(service.get('schema') == 1 and service.get('provider') == 'systemd-user'
            and binding.get('owner') == owner and binding.get('directory') == str(directory)
            and worker.get('kind') == 'guarded-worker-core' and worker.get('schema') == 1
            and worker.get('binding') == binding, 'WORKER_RECORD_BINDING_MISMATCH')
    require(binding.get('code_sha256') == actual, 'WORKER_SOURCE_BINDING_MISMATCH')
    require(service.get('persistence') in ('boot', 'session') and control.get('schema') == 1
            and control.get('desired') in ('running', 'paused')
            and (control.get('recovery') is None or isinstance(control.get('recovery'), str)), 'WORKER_RECORD_INVALID')
    unit = 'agent-project-ops-refresh-' + owner['owner'].replace('-', '') + '.service'
    require(service.get('unit_name') == unit, 'WORKER_UNIT_IDENTITY_MISMATCH')
    intent_path = management / 'worker/intent.json'
    intent = bp.read_small(intent_path) if intent_path.exists() or intent_path.is_symlink() else None
    result.update(status='saved_records_matched', persistence=service['persistence'],
                  desired_state=control['desired'], recovery_present=control['recovery'] is not None,
                  open_intent_present=bool(intent and intent.get('phase') != 'completed'), unit_name=unit,
                  _binding_fingerprint=hashlib.sha256(json.dumps([service, worker, control, intent], sort_keys=True).encode()).hexdigest())
    return result


def resource_metadata(limits, meminfo=None, cpus=None):
    if meminfo is None:
        with Path('/proc/meminfo').open() as stream: meminfo = stream.read(65537)
    require(len(meminfo) <= 65536, 'MEMINFO_LIMIT')
    values = {}
    for key in ('MemTotal', 'MemAvailable'):
        matches = re.findall(r'^' + key + r':\s+([0-9]+) kB$', meminfo, re.M)
        require(len(matches) == 1, 'MEMORY_METADATA_UNAVAILABLE')
        values[key] = int(matches[0])*1024
    require(0 <= values['MemAvailable'] <= values['MemTotal'] and values['MemTotal'] > 0, 'MEMORY_METADATA_INVALID')
    memories = [x['memory_limit_bytes'] for x in limits.values()]
    known = bool(memories) and all(type(x) is int and x > 0 for x in memories)
    required = sum(memories) if known else None
    reserve = (values['MemTotal'] + 9) // 10
    budget = max(0, values['MemAvailable']-reserve)
    if cpus is None: cpus = len(os.sched_getaffinity(0))
    require(type(cpus) is int and cpus > 0, 'CPU_METADATA_INVALID')
    return {'status': 'observed', 'memory_total_bytes': values['MemTotal'], 'memory_available_bytes': values['MemAvailable'],
            'current_process_cpu_affinity_count': cpus, 'container_limits': limits,
            'configured_restore_memory_limits_sum_bytes': required, 'proposed_host_memory_reserve_percent': 10,
            'memory_after_proposed_reserve_bytes': budget,
            'fits_existing_limits_estimate': None if required is None else budget >= required,
            'capacity_sufficient': None, 'resources_reserved': False, 'helper_memory_budget_included': False,
            'cpu_budget_confirmed': False, 'cgroup_limits_evaluated': False,
            'scope': 'current_proc_view_and_existing_container_limits_not_actual_restore_requirement'}


def historical_capacity(root):
    """Documented observation only: never use its values/paths as live configuration."""
    rel = 'docs/testing/ops-backup-capacity-server-20260929.json'
    value = bp.read_small(root / rel, private=False)
    require(value.get('schema') == 1 and value.get('stage') == 'O04-backup-capacity-server-observation'
            and value.get('result') == 'planned', 'HISTORICAL_CAPACITY_RECORD_INVALID')
    capacity = value['capacity']
    require(all(type(capacity.get(k)) is int and 0 < capacity[k] < 2**63 for k in
                ('per_generation_estimated_bytes', 'all_on_one_filesystem_estimated_bytes')), 'HISTORICAL_CAPACITY_RECORD_INVALID')
    require(isinstance(value.get('checked_at'), str) and
            re.fullmatch(r'[0-9T:.Z+-]{20,40}', value['checked_at']), 'HISTORICAL_CAPACITY_RECORD_INVALID')
    return {'status': 'historical_reference_only', 'record': rel, 'checked_at': value['checked_at'],
            'per_generation_estimated_bytes': capacity['per_generation_estimated_bytes'],
            'all_on_one_filesystem_estimated_bytes': capacity['all_on_one_filesystem_estimated_bytes'],
            'reused_as_current': False, 'source_sizes_remeasured': False, 'archive_size_verified': False}


def slim_destinations(value):
    rows = []
    for item in value['candidates']:
        fs = item.get('filesystem', {})
        relations = Counter(item.get('source_relations', {}).values())
        status, reasons = item['status'], list(item['reasons'])
        budget = item.get('available_after_proposed_reserve_bytes')
        if status == 'candidate':
            require(type(budget) is int and budget >= 0, 'DESTINATION_RESERVE_METADATA_INVALID')
            # No fresh archive estimate is available here. Even so, an exhausted
            # reserve leaves no space for any backup and cannot be a candidate.
            if budget == 0:
                status = 'rejected'
                reasons.append('NO_SPACE_AFTER_PROPOSED_RESERVE')
        rows.append({k: v for k, v in {
            'proposed_path': item.get('proposed_path'), 'parent': item['parent'], 'status': status,
            'reasons': reasons, 'error': item.get('error'), 'selected': False,
            'write_probe_executed': False, 'write_access_advisory': item.get('write_access_advisory'),
            'readonly': item.get('readonly'), 'fstype': fs.get('fstype'), 'device': fs.get('device'),
            'available_bytes': fs.get('available_bytes'),
            'available_after_proposed_reserve_bytes': item.get('available_after_proposed_reserve_bytes'),
            'source_relation_counts': dict(relations), 'capacity_sufficient': None,
            'independent_failure_domain_verified': False,
            'role_proposal': 'local_recovery_validation' if status == 'candidate' else None
        }.items()})
    return {'candidates': rows, 'scan_complete': value['scan_complete'], 'selected': False,
            'scope': value.get('discovery_scope'), 'network_filesystems_probed': False,
            'capacity_estimates_applied': False}


def inventory(root, lf_root, ops_root, docker, parents, reserve):
    """Reuse identity checks only; do not invoke capacity run/measure or database exec."""
    host = docker.host()
    mounts = bp.mount_table(Path('/proc/self/mountinfo').read_text())
    inv = cap.Inventory(root, lf_root, ops_root, docker, mounts)
    inv.host = host
    for component in bp.PROJECTS: getattr(inv, 'load_' + component)()
    before = inv.containers()
    require(len(before) == 9 and all(x.get('status') == 'matched' for x in before.values()), 'INVENTORY_IDENTITY_INCOMPLETE')
    exporters = inv.exporters()
    require(all(x['status'] == 'ok' for x in inv.files if x['required']), 'REQUIRED_FILE_METADATA_UNAVAILABLE')
    # Include source trees and whole control directories in destination overlap checks.
    sources = {x['name']: x['path'] for x in inv.stores}
    sources.update({x['name']: x['path'] for x in inv.files})
    sources['project-root'] = str(root)
    sources['ops-control-root'] = str(ops_root.parent)
    worker_before = worker_metadata(root, ops_root)
    limits = {name: docker.resources(row) for name, row in before.items()}
    resources = resource_metadata(limits)
    destinations = cap.find_candidates(mounts, Path.home(), sources, host['docker_root'], {}, extra=parents, reserve=reserve)
    # Cache contents/timestamp may advance independently; static configuration must not.
    for f in inv.files:
        if f['name'] == 'ops-cache/api-success.json': continue
        require(signature(bp.metadata(f['path'])) == f['metadata_signature'], 'PREPARED_METADATA_CHANGED')
    require(inv.containers() == before and inv.exporters() == exporters, 'CONTAINER_IDENTITY_CHANGED')
    require(worker_metadata(root, ops_root) == worker_before, 'WORKER_METADATA_CHANGED')
    require(bp.mount_table(Path('/proc/self/mountinfo').read_text()) == mounts, 'MOUNT_TABLE_CHANGED')
    worker_before.pop('_binding_fingerprint', None)
    return {'status': 'matched', 'core_container_count': len(before), 'exporter_count': len(exporters),
            'identity_stable': True, 'mount_table_stable': True, 'worker': worker_before,
            'resources': resources, 'destinations': slim_destinations(destinations)}


def run(root=ROOT, lf_root=None, ops_root=None, *, recipients=(), parents=(), reserve=10,
        outage_seconds=None, worker_pause_seconds=None, docker=None, age_probe=inspect_age, inventory_fn=inventory):
    docker = docker or Docker()
    lf_root = Path(lf_root or Path.home() / '.local/state/agent-project-langfuse')
    ops_root = Path(ops_root or Path.home() / '.local/state/agent-project-ops/server')
    report = {'schema': 1, 'stage': STAGE, 'result': 'attention_required', 'diagnostic_status': 'completed',
              'checked_at': bp.stamp(), 'scope': 'read_only_execution_prerequisites', 'backup_ready': False,
              'effects': {'files_written': [], 'service_control_requested': False, 'application_credential_contents_read': False,
                          'private_identity_read': False, 'application_api_requests': 0, 'docker_exec_commands': 0,
                          'key_generation_requested': False, 'encryption_requested': False, 'tool_installation_requested': False,
                          'directory_or_write_probe_requested': False, 'backup_executed': False, 'restore_executed': False,
                          'new_container_requested': False, 'image_build_or_pull_requested': False, 'inference_requested': False},
              'blockers': {'environment': [], 'decisions': [], 'implementation': [], 'acceptance': [
                  'DESTINATION_WRITE_PROBE_PENDING', 'KEY_CUSTODY_AND_DECRYPTION_PROOF_PENDING',
                  'CONSISTENCY_AND_RESTORE_ACCEPTANCE_PENDING', 'FRESH_EXECUTION_CAPACITY_PENDING']},
              'next_step': 'review_diagnostics_then_implement_scoped_worker_maintenance',
              'implementation_review': {'date': '2026-09-30', 'basis': 'reviewed_project_revision',
                                        'automatic_capability_activation': False},
              'reboot_acceptance': 'deferred_by_user'}
    blocks = report['blockers']
    try:
        require(type(reserve) is int and 0 <= reserve <= 50 and len(parents) <= 8, 'INVALID_PLAN_POLICY')
        require(all(x is None or type(x) is int and 1 <= x <= 86400 for x in (outage_seconds, worker_pause_seconds))
                and (outage_seconds is None or worker_pause_seconds is None or worker_pause_seconds >= outage_seconds),
                'INVALID_TIME_BUDGET')
        report['time_budgets'] = {'observation_outage_seconds': outage_seconds, 'worker_pause_seconds': worker_pause_seconds,
                                  'persisted': False, 'execution_authorized': False}
        if outage_seconds is None: blocks['decisions'].append('OBSERVATION_OUTAGE_BUDGET_NOT_SET')
        if worker_pause_seconds is None: blocks['decisions'].append('WORKER_PAUSE_BUDGET_NOT_SET')
        blocks['decisions'].append('DESTINATION_NOT_SELECTED')
        report['age'] = age_probe()
        if report['age']['status'] != 'observed': blocks['environment'].append('AGE_' + report['age']['status'].upper())
        else: blocks['acceptance'].append('AGE_BINARY_PROVENANCE_AND_COMPATIBILITY_PENDING')
        report['recipients'] = recipient_metadata(recipients)
        if report['recipients']['status'] != 'syntax_valid': blocks['decisions'].append('PUBLIC_RECIPIENT_' + report['recipients']['status'].upper())
        try: report['capacity_reference'] = historical_capacity(root)
        except Exception as exc: report['capacity_reference'] = {'status': 'unavailable', 'error': bp.code(exc), 'reused_as_current': False}
        data = inventory_fn(root, lf_root, ops_root, docker, parents, reserve)
        report['installation'] = {k: v for k, v in data.items() if k not in ('worker', 'resources', 'destinations')}
        for key in ('worker', 'resources', 'destinations'): report[key] = data[key]
        w, r, d = data['worker'], data['resources'], data['destinations']
        blocks['implementation'] = list(GAPS) if w['source_review'] == 'reviewed' else ['IMPLEMENTATION_SOURCE_REVIEW_REQUIRED']
        if w.get('desired_state') == 'paused' or w.get('recovery_present') or w.get('open_intent_present'):
            blocks['environment'].append('WORKER_SAVED_STATE_REQUIRES_REVIEW')
        if not d['scan_complete']: blocks['environment'].append('DESTINATION_DISCOVERY_INCOMPLETE')
        if not any(x['status'] == 'candidate' for x in d['candidates']): blocks['environment'].append('NO_USABLE_LOCAL_DESTINATION_CANDIDATE')
        if r['fits_existing_limits_estimate'] is False: blocks['environment'].append('MEMORY_BELOW_EXISTING_LIMITS_ESTIMATE')
        blocks['decisions'].append('RESTORE_RESOURCE_BUDGET_UNCONFIRMED')
    except Exception as exc:
        report.update(diagnostic_status='partial', error=bp.code(exc))
        blocks['environment'].append('READINESS_METADATA_INCOMPLETE')
    report['docker_metadata_commands'] = docker.calls + docker.base.calls
    require(docker.exec_calls == 0, 'READINESS_EXEC_BOUNDARY_VIOLATION')
    report['finished_at'] = bp.stamp()
    report['limits'] = ['no_backup_execution_or_current_worker_health_acceptance', 'no_source_size_rescan',
                        'historical_capacity_not_current_capacity', 'public_syntax_not_private_key_possession',
                        'version_and_local_hash_not_publisher_authentication', 'resource_snapshot_not_reservation',
                        'local_filesystem_metadata_IO_not_preemptible_in_uninterruptible_kernel_wait',
                        'existing_rootful_local_compose_adapter_scope_kubernetes_deferred']
    return report


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse errors can echo an accidentally supplied private identity.
        raise bp.Unavailable('INVALID_COMMAND_ARGUMENTS')


def main(argv=None):
    p = Parser(description=__doc__)
    p.add_argument('--langfuse-state-dir', type=Path)
    p.add_argument('--ops-state-dir', type=Path)
    p.add_argument('--recipient', action='append', default=[], help='optional native age public recipient only; never an identity')
    p.add_argument('--candidate-parent', type=Path, action='append', default=[])
    p.add_argument('--reserve-percent', type=int, default=10)
    p.add_argument('--observation-outage-seconds', type=int)
    p.add_argument('--worker-pause-seconds', type=int)
    try:
        args = p.parse_args(argv)
        require(sys.platform == 'linux' and os.getuid() != 0, 'RUN_AS_ORIGINAL_NONROOT_USER_NO_SUDO')
        report = run(lf_root=args.langfuse_state_dir, ops_root=args.ops_state_dir, recipients=args.recipient,
                     parents=args.candidate_parent, reserve=args.reserve_percent,
                     outage_seconds=args.observation_outage_seconds, worker_pause_seconds=args.worker_pause_seconds)
    except Exception as exc:
        report = {'schema': 1, 'stage': STAGE, 'result': 'attention_required', 'diagnostic_status': 'partial',
                  'backup_ready': False, 'error': bp.code(exc)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['diagnostic_status'] == 'completed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
