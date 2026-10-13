#!/usr/bin/env python3
"""Discover existing destination parents and estimate observability backup space; never write or back up."""
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('_capacity_db', ROOT / 'src/ops/tools/measure_backup_databases.py')
db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(db)
bp = db.bp
require = bp.require
LOCAL_FS = {'ext4', 'ext3', 'ext2', 'xfs', 'btrfs', 'zfs', 'f2fs', 'exfat', 'vfat', 'ntfs', 'ntfs3', 'fuseblk'}
IMAGE_FORMAT = bp.IMAGE_FORMAT


class Docker(db.Docker):
    """Same bounded client, owner-label discovery; no fixed Compose project or network address."""
    def containers_for(self, component, owner):
        label = bp.OWNERS[component]
        require(re.fullmatch(r'[0-9a-f-]{36}', owner), 'INVALID_OWNER')
        args = ['ps', '-aq', '--no-trunc', '--filter', 'label=' + label + '=' + owner]
        ids = self.metadata(args).split()
        require(len(ids) <= 32 and all(db.IDENT.fullmatch(i) for i in ids), 'INVALID_CONTAINER_IDS')
        names = ('id', 'image_id', 'image_ref', 'state', 'started_at', 'user', 'project', 'service', 'owner', 'mounts')
        rows = []
        for ident in ids:
            values = json.loads(self.metadata(['container', 'inspect', '--format', bp.container_format(component), ident]))
            require(isinstance(values, list) and len(values) == len(names), 'INVALID_CONTAINER_METADATA')
            row = dict(zip(names, values))
            require(row['id'] == ident, 'CONTAINER_ID_MISMATCH')
            rows.append(row)
        return rows

    def metadata(self, args):
        allowed = ((len(args) == 5 and args[:4] == ['ps', '-aq', '--no-trunc', '--filter'] and
                    any(re.fullmatch('label=' + re.escape(label) + r'=[0-9a-f-]{36}', args[4]) for label in bp.OWNERS.values())) or
                   (len(args) == 5 and args[:3] == ['container', 'inspect', '--format'] and
                    args[3] in {bp.container_format(c) for c in bp.PROJECTS} and db.IDENT.fullmatch(args[4])))
        require(allowed and self.base.endpoint is not None, 'METADATA_COMMAND_REFUSED')
        self.calls += 1
        rc, out, _ = self.runner(['docker', '--host', self.base.endpoint, *args], self.base.env, 20)
        require(rc == 0, 'DOCKER_METADATA_FAILED')
        return out.decode().strip()

    def logical_size(self, row, seconds):
        require(row['role'] in db.ROLES and row['target'] == db.ROLES[row['role']][1] and
                db.IDENT.fullmatch(row['container_id']) and type(row['uid']) is int and 0 < row['uid'] < 2**32 and
                type(row['gid']) is int and 0 <= row['gid'] < 2**32 and 1 <= seconds <= 60 and self.base.endpoint,
                'MEASUREMENT_TARGET_REFUSED')
        self.exec_calls += 1
        args = ['docker', '--host', self.base.endpoint, 'exec', '--user', f"{row['uid']}:{row['gid']}",
                '--workdir', '/', '--env', 'LC_ALL=C', row['container_id'], 'timeout', '-s', 'KILL', str(seconds),
                'du', '-b', '-s', '-x', row['target']]
        rc, out, err = self.runner(args, self.base.env, seconds + 10)
        require(rc not in (124, 137), 'MEASUREMENT_DEADLINE_EXCEEDED')
        require(rc == 0 and not err, 'LOGICAL_MEASUREMENT_FAILED')
        match = re.fullmatch(r'([0-9]{1,20})\s+' + re.escape(row['target']), out.decode().strip())
        require(match is not None and int(match[1]) <= 2**63-1, 'INVALID_SIZE_RESPONSE')
        return int(match[1])


class Inventory(bp.ComposeInventory):
    def exporters(self):
        pins = {name: self.prom['images'][name] for name in ('obs-node-exporter', 'obs-cadvisor')}
        gpu = bp.read_small(self.obs / 'gpu/.state.json')
        require(gpu['config_id'] == self.prom['config_id'] and gpu['enabled'] is True, 'GPU_CONFIGURATION_UNAVAILABLE')
        pins['obs-gpu'] = gpu['image']
        items = self.docker.containers_for('prometheus', self.prom['config_id'])
        result = {}
        for service, pin in pins.items():
            rows = [x for x in items if x['service'] == service]
            require(len(rows) == 1, 'EXPORTER_NOT_UNIQUE')
            row = rows[0]
            require(row['owner'] == self.prom['config_id'] and row['state'] == 'running' and
                    row['image_id'] == pin['image_id'] and row['image_ref'] == pin['digest'], 'EXPORTER_IDENTITY_MISMATCH')
            ident, digests, arch, size = self.docker.image(row['image_id'])
            require(ident == row['image_id'] and pin['digest'] in (digests or []) and type(size) is int and size >= 0 and
                    arch == {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(self.host['architecture'], self.host['architecture']), 'EXPORTER_IMAGE_MISMATCH')
            # Only exporter images/configuration. Never traverse host /, Docker data or GPU mounts.
            result[service] = dict(id=row['id'], image_id=ident, image_size_bytes=size,
                                   started_at=row['started_at'], project=row['project'], status='matched')
        return result

    def load_langfuse(self):
        # Owner/lock are non-secret. Do not open settings.json or credential env files.
        bp.metadata(self.lf_root, private=True, directory=True)
        owner = bp.read_small(self.lf_root / 'owner.json')['owner']
        require(str(bp.uuid.UUID(owner)) == owner, 'INVALID_OWNER')
        images = bp.read_small(self.root / 'src/infra/langfuse/images.lock.json', private=False)['images']
        for name in ('settings.json', 'owner.json', 'compose.env', 'postgres.env', 'clickhouse.env',
                     'redis.env', 'minio.env', 'worker.env', 'web.env'):
            self.file('langfuse-' + name, self.lf_root / name)
        for name, target in bp.STORES.items():
            service = 'lf-clickhouse' if name == 'clickhouse-logs' else 'lf-' + name
            self.store('langfuse-' + name, self.lf_root / 'data' / name, service, target, name == 'clickhouse-logs')
        for service, image in images.items():
            mounts = {row['container_target']: (row['path'], True) for row in self.stores if row['service'] == service}
            self.service(service, 'langfuse', owner, image['image'], mounts)
        self.store('langfuse-records', self.lf_root, None, None, optional=True)
        self.stores[-1]['exclude_children'] = ['data']

    def containers(self):
        result = {}
        for component in bp.PROJECTS:
            expected = {name: s for name, s in self.services.items() if s['component'] == component}
            try:
                require(bool(expected), 'COMPONENT_NOT_LOADED')
                owners = {s['owner'] for s in expected.values()}
                require(len(owners) == 1, 'OWNER_NOT_UNIQUE')
                items = self.docker.containers_for(component, next(iter(owners)))
                rows = [x for x in items if x['service'] in expected]
                require(sorted(x['service'] for x in rows) == sorted(expected), 'CONTAINER_SET_MISMATCH')
                projects = {x['project'] for x in rows}
                require(len(projects) == 1 and all(projects), 'PROJECT_NOT_UNIQUE')
                for x in rows:
                    s = expected[x['service']]
                    require(x['owner'] == s['owner'] and x['state'] == 'running', 'CONTAINER_OWNER_OR_STATE_MISMATCH')
                    canonical = lambda t: t.removeprefix('docker.io/')
                    require(canonical(x['image_ref']) == canonical(s['image']) and
                            (not s['image_id'] or x['image_id'] == s['image_id']), 'IMAGE_REFERENCE_MISMATCH')
                    require(s['user'] is None or x['user'] == s['user'], 'CONTAINER_USER_MISMATCH')
                    require(all(m['Destination'] == '/tmp' and component != 'langfuse' for m in x['mounts'] if m['Type'] == 'tmpfs'), 'UNEXPECTED_TMPFS')
                    mounts = [m for m in x['mounts'] if m['Type'] != 'tmpfs']
                    require(len(mounts) == len(s['mounts']) and all(m['Type'] == 'bind' for m in mounts) and
                            {m['Destination']: (m['Source'], m['RW']) for m in mounts} == s['mounts'], 'CONTAINER_MOUNT_MISMATCH')
                    image_id, digests, arch, size = self.docker.image(x['image_id'])
                    require(image_id == x['image_id'] and type(size) is int and size >= 0 and
                            arch == {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(self.host['architecture'], self.host['architecture']), 'IMAGE_IDENTITY_INVALID')
                    require('@sha256:' not in s['image'] or canonical(s['image']) in [canonical(d) for d in digests or []], 'IMAGE_DIGEST_UNAVAILABLE')
                    result[x['service']] = dict(id=x['id'], image_id=image_id, started_at=x['started_at'],
                                               project=x['project'], status='matched', image_size_bytes=size)
            except Exception as exc:
                result[component + '_issue'] = {'status': 'unavailable', 'error': bp.code(exc)}
        return result


def canonical_db_snapshot(snapshot):
    """Order-independent collections; retain every metadata field and duplicate."""
    result = copy.deepcopy(snapshot)
    key = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'))
    for row in result['containers']:
        row['mounts'] = sorted(row['mounts'], key=key)
    result['containers'] = sorted(result['containers'], key=key)
    result['stores'] = sorted(result['stores'], key=key)
    return result


def db_changed_fields(before, after):
    """Bounded field names only; never emit metadata values or arbitrary keys."""
    changes = []
    for name in ('directory_identity', 'owner'):
        if before.get(name) != after.get(name): changes.append(name)
    specs = (
        ('containers', 'service', ('lf-postgres', 'lf-clickhouse'),
         ('id', 'image_id', 'image_ref', 'state', 'started_at', 'owner', 'project', 'user', 'userns', 'mounts')),
        ('stores', 'role', tuple(db.ROLES),
         ('source', 'target', 'container_id', 'image_id', 'uid', 'gid', 'identity', 'optional_diagnostic')))
    for collection, selector, roles, fields in specs:
        old, new = before.get(collection, []), after.get(collection, [])
        if old == new: continue
        if (sorted(x.get(selector, '') for x in old) != sorted(x.get(selector, '') for x in new) or
                any(sum(x.get(selector) == role for x in rows) != 1 for rows in (old, new) for role in roles) or
                any(x.get(selector) not in roles for rows in (old, new) for x in rows)):
            changes.append(collection + '.membership')
            continue
        for role in roles:
            a = next(x for x in old if x[selector] == role)
            b = next(x for x in new if x[selector] == role)
            for field in fields:
                if (field in a) != (field in b) or a.get(field) != b.get(field):
                    changes.append(collection + '.' + role + '.' + field)
            if ({k: v for k, v in a.items() if k not in (*fields, selector)} !=
                    {k: v for k, v in b.items() if k not in (*fields, selector)}):
                changes.append(collection + '.' + role + '.other_metadata')
    if ({k: v for k, v in before.items() if k not in ('directory_identity', 'owner', 'containers', 'stores')} !=
            {k: v for k, v in after.items() if k not in ('directory_identity', 'owner', 'containers', 'stores')}):
        changes.append('other_metadata')
    return changes or (['snapshot_metadata'] if before != after else [])


def compare_db_discovery(probe, baseline, phase):
    try:
        # Existing owner/image/mount/UID validation still runs before comparison.
        current = canonical_db_snapshot(probe.discover())
        fields = db_changed_fields(baseline, current)
        return {'phase': phase, 'status': 'changed' if fields else 'stable', 'changed_fields': fields}
    except Exception as exc:
        return {'phase': phase, 'status': 'unavailable', 'changed_fields': [], 'error': bp.code(exc)}


def overlap(a, b):
    return Path(a).is_relative_to(b) or Path(b).is_relative_to(a)


def lexical_path(path):
    # Mount classification must precede even lstat/is_symlink on a remote path.
    path = Path(path).expanduser()
    require(path.is_absolute() and '..' not in path.parts and
            not any(ord(c) < 32 or ord(c) == 127 for c in str(path)), 'INVALID_PATH')
    return path


def estimate(rows, images, generations=2, headroom=20):
    require(1 <= generations <= 30 and 0 <= headroom <= 100, 'INVALID_PLAN_POLICY')
    missing = [x['name'] for x in rows if x.get('status') != 'complete' or type(x.get('logical_bytes')) is not int]
    valid = all(type(x.get('image_size_bytes')) is int and x['image_size_bytes'] >= 0 for x in images.values()) and bool(images)
    result = {'status': 'incomplete', 'missing_sources': missing, 'image_metadata_complete': valid,
              'total_required_bytes': None, 'capacity_sufficient': None, 'archive_size_verified': False,
              'generations': generations, 'headroom_percent': headroom, 'workspace_copies': 1, 'restore_copies': 1,
              'compression_savings_assumed': False, 'defaults_are_proposals': True}
    if missing or not valid:
        return result
    logical = sum(x['logical_bytes'] for x in rows)
    unique = {}
    for x in images.values():
        require(x['image_id'] not in unique or unique[x['image_id']] == x['image_size_bytes'], 'IMAGE_SIZE_CONFLICT')
        unique[x['image_id']] = x['image_size_bytes']
    image_bytes = sum(unique.values())
    copy_bytes = ((logical + image_bytes) * (100 + headroom) + 99) // 100
    result.update(status='estimated', live_logical_source_bytes=logical,
                  unique_image_count=len(unique), image_uncompressed_size_proxy_bytes=image_bytes,
                  per_generation_estimated_bytes=copy_bytes,
                  retained_archives_estimated_bytes=generations * copy_bytes,
                  staging_estimated_bytes=copy_bytes, restore_estimated_bytes=copy_bytes,
                  destination_with_staging_estimated_bytes=(generations + 1) * copy_bytes,
                  all_on_one_filesystem_estimated_bytes=(generations + 2) * copy_bytes,
                  basis='live_logical_sizes_plus_unique_image_Size_with_proposed_headroom_not_export_measurement')
    return result


def candidate(parent, sources, docker_root, mounts, capacity, reserve=10):
    result = {'parent': str(parent), 'selected': False, 'write_probe_executed': False, 'reasons': []}
    try:
        parent = lexical_path(parent)
        matching = [m for m in mounts if parent.is_relative_to(m['mountpoint'])]
        require(bool(matching) and max(matching, key=lambda m: len(m['mountpoint']))['fstype'] in LOCAL_FS,
                'UNSUPPORTED_DESTINATION_FILESYSTEM')
        parent = bp.safe(parent)
        bp.metadata(parent, directory=True)
        dest = bp.safe(parent / 'agent-project-observability-backups')
        require(not dest.exists() or dest.is_dir(), 'DESTINATION_NOT_DIRECTORY')
        exists = dest.exists()
        inspect = dest if exists else parent
        identity = db.signature(bp.metadata(inspect, directory=True))
        fs = bp.filesystem(inspect, mounts)
        require(fs['fstype'] in LOCAL_FS, 'UNSUPPORTED_DESTINATION_FILESYSTEM')
        v = os.statvfs(inspect)
        readonly = fs['readonly'] or bool(v.f_flag & os.ST_RDONLY)
        access = os.access(inspect, os.W_OK | os.X_OK, effective_ids=True)
        relations = {}
        for name, source in sources.items():
            other = bp.filesystem(source, mounts)
            relation = 'unknown'
            if other['device'] == fs['device']:
                relation = 'same_filesystem'
            elif other.get('physical_disks') and fs.get('physical_disks'):
                relation = ('same_physical_disk' if set(other['physical_disks']) & set(fs['physical_disks'])
                            else 'different_block_devices_same_host')
            relations[name] = relation
        result.update(proposed_path=str(dest), path_exists=dest.exists(), filesystem=fs,
                      write_access_advisory=access, readonly=readonly, source_relations=relations,
                      available_inodes=v.f_favail if v.f_files else None,
                      restore_inode_sufficiency='not_measured', archive_encryption_required=True,
                      restore_filesystem_compatibility='unverified',
                      status='candidate', capacity_sufficient=None, reserve_percent=reserve)
        if readonly: result['reasons'].append('READ_ONLY_FILESYSTEM')
        if not access: result['reasons'].append('PARENT_NOT_WRITABLE_BY_CURRENT_USER')
        if any(overlap(dest, p) for p in [*sources.values(), docker_root]): result['reasons'].append('SOURCE_OR_DOCKER_PATH_OVERLAP')
        if v.f_files and v.f_favail == 0: result['reasons'].append('NO_AVAILABLE_INODES')
        if fs['fstype'] == 'vfat' and (capacity.get('per_generation_estimated_bytes') is None or
                                     capacity['per_generation_estimated_bytes'] >= 2**32):
            result['reasons'].append('FAT32_ARCHIVE_SIZE_LIMIT_UNRESOLVED')
        budget = max(0, fs['available_bytes'] - (fs['total_bytes'] * reserve + 99) // 100)
        result['available_after_proposed_reserve_bytes'] = budget
        estimated = capacity.get('all_on_one_filesystem_estimated_bytes')
        result['fits_colocated_estimate'] = None if estimated is None else budget >= estimated
        if estimated is not None and budget < estimated: result['reasons'].append('INSUFFICIENT_FOR_PROPOSED_ESTIMATE')
        if result['reasons']: result['status'] = 'rejected'
        require(db.signature(bp.metadata(inspect, directory=True)) == identity and dest.exists() == exists,
                'DESTINATION_METADATA_CHANGED')
    except Exception as exc:
        result.update(status='unavailable', fits_colocated_estimate=None, error=bp.code(exc))
    return result


def find_candidates(mounts, home, sources, docker_root, capacity, extra=(), reserve=10, limit=64):
    require(0 <= reserve <= 50 and len(extra) <= 8, 'INVALID_DESTINATION_POLICY')
    parents = {str(lexical_path(home))}
    exclusions = []
    incomplete = False
    start = time.monotonic()
    for mount in mounts:
        p = Path(mount['mountpoint'])
        if mount['fstype'] not in LOCAL_FS or any(p.is_relative_to(x) for x in ('/proc', '/sys', '/dev', '/run')):
            continue
        if any(p.is_relative_to(x) for x in [docker_root, *sources.values()]):
            exclusions.append({'path': str(p), 'reason': 'SOURCE_OR_DOCKER_SUBTREE'})
            continue
        parents.add(str(p))
        # Only immediate owned directories; never recurse a mount looking for a place to write.
        if p == Path('/') or mount['readonly']:
            continue
        try:
            fd = bp.open_directory(p)
            try:
                with os.scandir(fd) as scan:
                    for n, entry in enumerate(scan):
                        if n >= 64 or len(parents) >= limit or time.monotonic() - start > 10:
                            incomplete = True
                            break
                        st = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not entry.name.startswith('.'):
                            child = p / entry.name
                            if not any(overlap(child, x) for x in [docker_root, *sources.values()]): parents.add(str(child))
            finally:
                os.close(fd)
        except (OSError, bp.Unavailable):
            exclusions.append({'path': str(p), 'reason': 'CHILD_DIRECTORY_DISCOVERY_UNAVAILABLE'})
        if len(parents) >= limit:
            incomplete = True
            break
    for p in extra: parents.add(str(lexical_path(p)))
    if len(parents) > limit: incomplete = True
    rows = [candidate(Path(p), sources, docker_root, mounts, capacity, reserve) for p in sorted(parents)[:limit]]
    return {'candidates': rows, 'scan_complete': not incomplete, 'excluded': exclusions,
            'automatic_selection': False, 'network_filesystems_probed': False,
            'discovery_scope': 'mounted_local_filesystems_home_and_bounded_immediate_owned_directories'}


def host_config_paths():
    """Existing system configuration metadata only, no file contents or systemctl."""
    paths = [Path('/etc/fstab')]
    daemon = Path('/etc/docker/daemon.json')
    if daemon.exists(): paths.append(daemon)
    directory = Path('/etc/systemd/system/docker.service.d')
    if directory.exists():
        bp.safe(directory)
        files = sorted(directory.glob('*.conf'))
        require(len(files) <= 32, 'HOST_DROPIN_COUNT_LIMIT')
        paths.extend(files)
    return paths


def run(root, lf_root, ops_root, docker=None, seconds=15, generations=2, headroom=20, reserve=10, extra=()):
    docker = docker or Docker()
    report = {'schema': 1, 'stage': 'O04-backup-destination-capacity-plan', 'result': 'attention_required',
              'checked_at': bp.stamp(), 'backup_ready': False, 'destination_selected': False,
              'effects': {'files_written': [], 'service_control_requested': False, 'backup_executed': False,
                          'restore_executed': False, 'application_api_requests': 0, 'inference_requested': False,
                          'new_container_requested': False, 'image_build_or_pull_requested': False,
                          'permissions_changed': False, 'mounts_changed': False, 'credential_contents_read': False},
              'reboot_acceptance': 'deferred_by_user'}
    inv = None
    try:
        require(1 <= seconds <= 60, 'INVALID_SCAN_LIMIT')
        host = docker.host()
        mounts = bp.mount_table(Path('/proc/self/mountinfo').read_text())
        inv = Inventory(root, lf_root, ops_root, docker, mounts, seconds)
        inv.host = host
        components = {}
        for comp in bp.PROJECTS:
            try:
                getattr(inv, 'load_' + comp)()
                components[comp] = 'matched'
            except Exception as exc:
                components[comp] = bp.code(exc)
        report['components'] = components
        before = inv.containers()
        report['containers'] = before
        require(all(x == 'matched' for x in components.values()) and len(before) == 9 and
                all(x.get('status') == 'matched' for x in before.values()), 'INVENTORY_IDENTITY_INCOMPLETE')
        exporters = inv.exporters()
        report['exporters'] = exporters
        ops_owner = bp.read_small(Path(ops_root) / 'owner.json')
        owner = ops_owner['owner']
        management = Path(ops_root).parent / 'auto-refresh' / owner
        if management.exists(): inv.store('ops-auto-refresh-records', management, None, None)
        for sub in ('src/ops/platform', 'src/ops/tools', 'src/ops/candidates', 'src/infra/observability', 'src/infra/langfuse'):
            inv.store('source-' + sub.replace('/', '-'), Path(root) / sub, None, None)
        # Source trees include their own configuration files; external references are separate.
        if management.exists():
            service_record = bp.read_small(management / 'service.json')
            require(service_record.get('binding', {}).get('owner') == ops_owner and
                    service_record['binding'].get('directory') == str(ops_root), 'SERVICE_RECORD_OWNER_MISMATCH')
            unit = bp.safe(service_record['unit_path'])
            require(unit.name == 'agent-project-ops-refresh-' + owner.replace('-', '') + '.service', 'UNIT_PATH_IDENTITY_MISMATCH')
            st = bp.metadata(unit)
            require(st.st_uid == os.getuid() and not st.st_mode & 0o022, 'UNIT_FILE_OWNER_MODE_MISMATCH')
            inv.file('owned-user-unit-file', unit, private=False)
            inv.file('owned-service-record', management / 'service.json')
        for i, path in enumerate(host_config_paths()):
            inv.file('host-configuration-' + str(i), path, private=False)
        report['files'] = [{k: v for k, v in x.items() if k != 'metadata_signature'} for x in inv.files]
        require(all(x['status'] == 'ok' for x in inv.files if x['required']), 'REQUIRED_FILE_METADATA_UNAVAILABLE')
        roots = [Path(x['path']) for x in inv.stores]
        # Prevent duplicate source accounting except deliberate records/data exclusions.
        for a in inv.stores:
            for b in inv.stores:
                if a is b or not Path(b['path']).is_relative_to(a['path']): continue
                part = Path(b['path']).relative_to(a['path']).parts
                require(part and part[0] in a.get('exclude_children', []), 'SOURCE_ROOT_OVERLAP')
        sources = {x['name']: x['path'] for x in inv.stores}
        db_probe = db.Measurement(lf_root, docker, root, seconds)
        db_probe.host, db_probe.mounts = host, mounts
        db_before = canonical_db_snapshot(db_probe.discover())
        db_checks = []
        report['database_discovery'] = {'comparison': 'unordered_collections_all_metadata_preserved', 'checks': db_checks}
        db_rows = {x['role']: x for x in db_before['stores']}
        sizes = []
        for store in inv.stores:
            name, path = store['name'], Path(store['path'])
            row = {'name': name, 'path': str(path), 'status': 'unavailable', 'logical_bytes': None,
                   'measured_at': bp.stamp(), 'consistent_snapshot': False}
            sizes.append(row)
            try:
                role = name.removeprefix('langfuse-')
                if role in db_rows:
                    check = compare_db_discovery(db_probe, db_before, 'before_' + role)
                    db_checks.append(check)
                    require(check['status'] == 'stable', check.get('error', 'DB_DISCOVERY_CHANGED'))
                    data = db_rows[role]
                    db.check_stat(docker.execute(data, 'stat', 5), data)
                    value = docker.logical_size(data, seconds)
                    db.check_stat(docker.execute(data, 'stat', 5), data)
                    row.update(status='complete', logical_bytes=value, method='du_apparent_bytes_including_directory_overhead')
                else:
                    size = bp.measure(path, mounts, seconds, 200000, store.get('exclude_children', []))
                    row.update(status=size['status'], logical_bytes=size['logical_bytes'],
                               method='regular_file_metadata_excludes_directory_overhead', issues=size['issues'])
            except Exception as exc:
                row['error'] = bp.code(exc)
        external_seen = set()
        for f in inv.files:
            if any(Path(f['path']).is_relative_to(p) for p in roots) or f['path'] in external_seen: continue
            external_seen.add(f['path'])
            sizes.append({'name': f['name'], 'path': f['path'], 'status': 'complete', 'logical_bytes': f['bytes'],
                          'method': 'file_stat_only', 'consistent_snapshot': False})
            sources[f['name']] = f['path']
        report['sources'] = sizes
        changed = []
        for f in inv.files:
            if f['name'] == 'ops-cache/api-success.json': continue
            s = bp.metadata(f['path'])
            if [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns] != f['metadata_signature']: changed.append(f['name'])
        core_stable = inv.containers() == before
        exporters_stable = inv.exporters() == exporters
        db_checks.append(compare_db_discovery(db_probe, db_before, 'before_capacity'))
        report['identity_checks'] = {'core_containers': core_stable, 'exporters': exporters_stable,
                                    'database_discovery': all(x['status'] == 'stable' for x in db_checks),
                                    'configuration_metadata': not changed}
        stable = all(report['identity_checks'].values())
        report['identity_stable'] = stable
        report['configuration_metadata_changed'] = changed
        require(stable, 'INVENTORY_CHANGED_DURING_PLAN')
        report['capacity'] = estimate(sizes, {**before, **exporters}, generations, headroom)
        report['destinations'] = find_candidates(mounts, Path.home(), sources, host['docker_root'], report['capacity'], extra, reserve)
        after_mounts = bp.mount_table(Path('/proc/self/mountinfo').read_text())
        report['mount_table_stable'] = after_mounts == mounts
        require(report['mount_table_stable'], 'MOUNT_TABLE_CHANGED')
        require(inv.containers() == before and inv.exporters() == exporters, 'CONTAINER_CHANGED_DURING_DESTINATION_DISCOVERY')
        report['result'] = ('planned' if report['capacity']['status'] == 'estimated' and
                            report['destinations']['scan_complete'] and any(x['status'] == 'candidate' for x in report['destinations']['candidates']) else 'attention_required')
        report['host'] = host
    except Exception as exc:
        report['error'] = bp.code(exc)
        if 'capacity' in report: report['capacity']['status'] = 'invalidated'
        if 'destinations' in report:
            for row in report['destinations']['candidates']:
                row.update(status='unavailable', fits_colocated_estimate=None, error='FINAL_IDENTITY_NOT_VERIFIED')
    report['docker_exec_commands'] = docker.exec_calls
    report['docker_metadata_commands'] = docker.calls + docker.base.calls
    report['finished_at'] = bp.stamp()
    report['limits'] = ['proposal_not_backup_or_restore_acceptance', 'live_sizes_not_consistent_snapshot',
                        'image_Size_not_docker_save_archive_size', 'headroom_is_not_a_guaranteed_upper_bound',
                        'same_host_different_devices_not_offsite_backup', 'write_access_is_advisory_no_probe',
                        'core_nine_services_plus_three_exporter_images_code_worker_records_and_selected_host_metadata',
                        'host_OS_packages_user_accounts_linger_unit_links_and_optional_CLI_cache_not_fully_inventoried',
                        'local_metadata_scan_budget_cannot_interrupt_uninterruptible_kernel_IO']
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--langfuse-state-dir', type=Path, default=Path.home() / '.local/state/agent-project-langfuse')
    p.add_argument('--ops-state-dir', type=Path, default=Path.home() / '.local/state/agent-project-ops/server')
    p.add_argument('--candidate-parent', type=Path, action='append', default=[])
    p.add_argument('--max-seconds-per-store', type=int, default=15)
    p.add_argument('--generations', type=int, default=2)
    p.add_argument('--headroom-percent', type=int, default=20)
    p.add_argument('--reserve-percent', type=int, default=10)
    a = p.parse_args(argv)
    try:
        require(sys.platform == 'linux' and os.getuid() != 0, 'RUN_AS_ORIGINAL_NONROOT_USER_NO_SUDO')
        require(1 <= a.generations <= 30 and 0 <= a.headroom_percent <= 100 and
                0 <= a.reserve_percent <= 50 and len(a.candidate_parent) <= 8, 'INVALID_PLAN_POLICY')
        report = run(ROOT, a.langfuse_state_dir, a.ops_state_dir, seconds=a.max_seconds_per_store,
                     generations=a.generations, headroom=a.headroom_percent, reserve=a.reserve_percent, extra=a.candidate_parent)
    except Exception as exc:
        report = {'result': 'attention_required', 'error': bp.code(exc), 'backup_ready': False, 'destination_selected': False}
    print(bp.render(report))
    return 0 if report['result'] == 'planned' else 2


if __name__ == '__main__':
    raise SystemExit(main())
