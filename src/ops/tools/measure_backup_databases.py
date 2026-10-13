#!/usr/bin/env python3
"""Measure allocated DB storage through verified existing containers; never back up data."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('_backup_metadata', ROOT / 'src/infra/observability/ops/backup_preflight.py')
bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bp)
LABEL = 'org.agent-project.langfuse.owner'
ROLES = {'postgres': ('lf-postgres', '/var/lib/postgresql/data'),
         'clickhouse': ('lf-clickhouse', '/var/lib/clickhouse'),
         'clickhouse-logs': ('lf-clickhouse', '/var/log/clickhouse-server')}
FORMAT = '[' + ','.join('{{json ' + x + '}}' for x in (
    '.Id', '.Image', '.Config.Image', '.State.Status', '.State.StartedAt',
    '(index .Config.Labels "' + LABEL + '")',
    '(index .Config.Labels "com.docker.compose.project")',
    '(index .Config.Labels "com.docker.compose.service")', '.Mounts', '.HostConfig.UsernsMode')) + ']'
FIELDS = ('id', 'image_id', 'image_ref', 'state', 'started_at', 'owner', 'project', 'service', 'mounts', 'userns')
IDENT = re.compile(r'[0-9a-f]{64}')
IMAGE = re.compile(r'sha256:[0-9a-f]{64}')
STAT_FORMAT = '%d:%i:%u:%g:%f'
require = bp.require


def bounded(argv, env, seconds):
    """Bound client time and output, never expose stderr; no shell or saved-PID signalling."""
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, start_new_session=True)
    except OSError:
        raise bp.Unavailable('COMMAND_UNAVAILABLE') from None
    buffers = {'out': bytearray(), 'err': bytearray()}
    end = time.monotonic() + seconds
    try:
        with selectors.DefaultSelector() as selector:
            for stream, name in ((process.stdout, 'out'), (process.stderr, 'err')):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = end - time.monotonic()
                require(remaining > 0, 'CLIENT_DEADLINE_EXCEEDED')
                for key, _ in selector.select(min(remaining, 0.25)):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        buffers[key.data].extend(chunk)
                        require(sum(map(len, buffers.values())) <= 256 * 1024, 'COMMAND_OUTPUT_LIMIT')
            process.wait(timeout=max(0.01, end - time.monotonic()))
        return process.returncode, bytes(buffers['out']), bytes(buffers['err'])
    except subprocess.TimeoutExpired:
        raise bp.Unavailable('CLIENT_DEADLINE_EXCEEDED') from None
    finally:
        # Also terminates local descendants holding pipes if the CLI has exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()
        process.stderr.close()


class Docker:
    def __init__(self, runner=bounded):
        self.runner = runner
        self.base = bp.DockerReadOnly()
        self.calls = 0
        self.exec_calls = 0

    def host(self):
        host = self.base.host()
        # Numeric container users must map directly to the discovered host owners.
        raw = self.read(['info', '--format', bp.INFO_FORMAT])
        security = json.loads(raw)[4] or []
        require(not any('userns' in x for x in security), 'USER_NAMESPACE_MAPPING_UNSUPPORTED')
        return host

    def read(self, args):
        allowed = (args == ['info', '--format', bp.INFO_FORMAT] or
                   (len(args) == 5 and args[:4] == ['ps', '-aq', '--no-trunc', '--filter'] and
                    re.fullmatch('label=' + re.escape(LABEL) + r'=[0-9a-f-]{36}', args[4])) or
                   (len(args) == 5 and args[:3] == ['container', 'inspect', '--format'] and
                    args[3] == FORMAT and IDENT.fullmatch(args[4])) or
                   (len(args) == 5 and args[:3] == ['image', 'inspect', '--format'] and
                    args[3] == bp.IMAGE_FORMAT and IMAGE.fullmatch(args[4])))
        require(allowed and self.base.endpoint is not None, 'METADATA_COMMAND_REFUSED')
        self.calls += 1
        rc, out, _ = self.runner(['docker', '--host', self.base.endpoint, *args], self.base.env, 20)
        require(rc == 0, 'DOCKER_METADATA_FAILED')
        return out.decode('utf-8').strip()

    def containers(self, owner):
        require(str(uuid.UUID(owner)) == owner, 'INVALID_OWNER')
        ids = self.read(['ps', '-aq', '--no-trunc', '--filter', 'label=' + LABEL + '=' + owner]).split()
        require(len(ids) <= 16 and all(IDENT.fullmatch(x) for x in ids), 'CONTAINER_DISCOVERY_INVALID')
        rows = []
        for ident in ids:
            value = json.loads(self.read(['container', 'inspect', '--format', FORMAT, ident]))
            require(isinstance(value, list) and len(value) == len(FIELDS), 'INVALID_CONTAINER_METADATA')
            row = dict(zip(FIELDS, value))
            require(row['id'] == ident, 'CONTAINER_ID_MISMATCH')
            rows.append(row)
        return rows

    def image(self, ident):
        return json.loads(self.read(['image', 'inspect', '--format', bp.IMAGE_FORMAT, ident]))

    def execute(self, row, operation, seconds):
        require(operation in ('stat', 'du') and 1 <= seconds <= 60, 'MEASUREMENT_COMMAND_REFUSED')
        role = row['role']
        require(role in ROLES and row['target'] == ROLES[role][1] and IDENT.fullmatch(row['container_id']),
                'MEASUREMENT_TARGET_REFUSED')
        uid, gid = row['uid'], row['gid']
        require(type(uid) is int and 0 < uid < 2**32 and type(gid) is int and 0 <= gid < 2**32,
                'NONROOT_STORAGE_OWNER_REQUIRED')
        command = (['stat', '-c', STAT_FORMAT, row['target']] if operation == 'stat'
                   else ['du', '-k', '-s', '-x', row['target']])
        # Both client and in-container commands are finite. No injected shell/script/files.
        argv = ['docker', '--host', self.base.endpoint, 'exec', '--user', f'{uid}:{gid}',
                '--workdir', '/', '--env', 'LC_ALL=C', row['container_id'],
                'timeout', '-s', 'KILL', str(seconds), *command]
        require(self.base.endpoint is not None, 'LOCAL_DOCKER_NOT_VERIFIED')
        self.exec_calls += 1
        rc, out, err = self.runner(argv, self.base.env, seconds + 10)
        if rc in (124, 137):
            raise bp.Unavailable('MEASUREMENT_DEADLINE_EXCEEDED')
        if rc in (126, 127):
            raise bp.Unavailable('CONTAINER_MEASUREMENT_TOOLS_UNAVAILABLE')
        require(rc == 0 and not err, 'MEASUREMENT_FAILED')
        return out.decode('utf-8').strip()


def signature(st):
    return (st.st_dev, st.st_ino, st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode))


def check_stat(text, row):
    require(re.fullmatch(r'[0-9]+:[0-9]+:[0-9]+:[0-9]+:[0-9a-fA-F]+', text), 'INVALID_STORAGE_STAT')
    d, inode, uid, gid, mode = text.split(':')
    mode = int(mode, 16)
    require(stat.S_ISDIR(mode), 'STORAGE_NOT_DIRECTORY')
    require((int(d), int(inode), int(uid), int(gid), stat.S_IMODE(mode)) == tuple(row['identity']),
            'CONTAINER_STORAGE_IDENTITY_MISMATCH')


def parse_size(text, target):
    match = re.fullmatch(r'([0-9]{1,20})\s+' + re.escape(target), text)
    require(match is not None, 'INVALID_SIZE_RESPONSE')
    kib = int(match[1])
    require(kib <= (2**63 - 1) // 1024, 'INVALID_SIZE_RESPONSE')
    return kib * 1024


class Measurement:
    def __init__(self, directory, docker=None, root=ROOT, seconds=30):
        self.directory = bp.safe(directory)
        self.root = Path(root)
        self.docker = docker or Docker()
        self.seconds = seconds

    def discover(self):
        require(1 <= self.seconds <= 60, 'INVALID_DEADLINE')
        root_st = bp.metadata(self.directory, private=True, directory=True)
        owner = bp.read_small(self.directory / 'owner.json')
        require(set(owner) == {'owner'} and str(uuid.UUID(owner['owner'])) == owner['owner'], 'INVALID_OWNER')
        # These files have no passwords/keys; settings.json and *.env are not opened.
        locks = bp.read_small(self.root / 'src/infra/langfuse/images.lock.json', private=False)['images']
        all_rows = self.docker.containers(owner['owner'])
        rows, stores = [], []
        for service in ('lf-postgres', 'lf-clickhouse'):
            matches = [x for x in all_rows if x['service'] == service]
            require(len(matches) == 1, 'DATABASE_CONTAINER_NOT_UNIQUE')
            row = matches[0]
            require(row['owner'] == owner['owner'] and row['state'] == 'running' and row['project'],
                    'DATABASE_OWNERSHIP_OR_STATE_MISMATCH')
            require(IDENT.fullmatch(row['id']) and IMAGE.fullmatch(row['image_id']), 'INVALID_CONTAINER_IDENTITY')
            require(row['userns'] in ('', 'host'), 'USER_NAMESPACE_MAPPING_UNSUPPORTED')
            canonical = lambda value: value.removeprefix('docker.io/')
            expected = locks[service]['image']
            require('@sha256:' in expected and canonical(row['image_ref']) == canonical(expected),
                    'DATABASE_IMAGE_MISMATCH')
            image_id, digests, arch, _ = self.docker.image(row['image_id'])
            require(image_id == row['image_id'] and canonical(expected) in [canonical(x) for x in digests or []],
                    'DATABASE_IMAGE_MISMATCH')
            require(arch == {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(self.host['architecture'], self.host['architecture']),
                    'DATABASE_ARCHITECTURE_MISMATCH')
            expected_mounts = {target: str(self.directory / 'data' / role)
                               for role, (svc, target) in ROLES.items() if svc == service}
            require(len(row['mounts']) == len(expected_mounts) and
                    all(x.get('Type') == 'bind' and x.get('RW') is True for x in row['mounts']) and
                    {x['Destination']: x['Source'] for x in row['mounts']} == expected_mounts,
                    'DATABASE_MOUNT_MISMATCH')
            rows.append(row)
            for role, (svc, target) in ROLES.items():
                if svc != service:
                    continue
                path = bp.safe(self.directory / 'data' / role)
                st = bp.metadata(path, directory=True)
                require(st.st_uid > 0, 'NONROOT_STORAGE_OWNER_REQUIRED')
                require(not path.is_relative_to(self.host['docker_root']), 'DOCKER_ROOT_SCAN_REFUSED')
                require(not any(Path(m['mountpoint']) != path and Path(m['mountpoint']).is_relative_to(path)
                                for m in self.mounts), 'NESTED_MOUNT_REFUSED')
                stores.append(dict(role=role, source=str(path), target=target, container_id=row['id'],
                                   image_id=row['image_id'], uid=st.st_uid, gid=st.st_gid,
                                   identity=signature(st), optional_diagnostic=(role == 'clickhouse-logs')))
        require(rows[0]['project'] == rows[1]['project'], 'DATABASE_PROJECT_MISMATCH')
        # Snapshot includes all metadata that authorizes the subsequent fixed commands.
        return {'directory_identity': signature(root_st), 'owner': owner, 'containers': rows, 'stores': stores}

    def run(self):
        report = dict(schema=1, stage='O04-observability-database-sizes', result='attention_required',
                      checked_at=bp.stamp(), scope='live_allocated_metadata_in_existing_owned_database_containers',
                      storage=[], backup_executed=False, restore_executed=False, backup_ready=False,
                      destination_selected=False, production_credential_contents_read=False,
                      application_api_requests=0, inference_requested=False, service_control_requested=False,
                      image_build_or_pull_requested=False, new_container_requested=False,
                      filesystem_writes_requested=False, permissions_changed=False,
                      existing_container_measurement_processes_requested=False,
                      internal_deadline_seconds=self.seconds, consistent_snapshot=False,
                      reboot_acceptance='deferred_by_user', temporary_files_created=False)
        baseline = None
        try:
            self.host = self.docker.host()
            self.mounts = bp.mount_table(Path('/proc/self/mountinfo').read_text())
            baseline = self.discover()
            report['discovery'] = {'owner': baseline['owner']['owner'],
                                   'compose_project': baseline['containers'][0]['project'],
                                   'container_count': 2, 'state_dir': str(self.directory)}
            for store in baseline['stores']:
                result = {k: store[k] for k in ('role', 'source', 'target', 'container_id', 'image_id', 'uid', 'gid', 'optional_diagnostic')}
                result.update(status='unavailable', allocated_bytes=None, logical_bytes=None,
                              size_semantics='du_allocated_rounded_up_to_1024_includes_directory_overhead',
                              consistent_snapshot=False)
                report['storage'].append(result)
                try:
                    require(self.discover() == baseline, 'DISCOVERY_CHANGED_BEFORE_MEASUREMENT')
                    report['existing_container_measurement_processes_requested'] = True
                    check_stat(self.docker.execute(store, 'stat', 5), store)
                    value = parse_size(self.docker.execute(store, 'du', self.seconds), store['target'])
                    check_stat(self.docker.execute(store, 'stat', 5), store)
                    require(signature(bp.metadata(store['source'], directory=True)) == tuple(store['identity']),
                            'STORAGE_CHANGED_DURING_MEASUREMENT')
                    result.update(status='measured', allocated_bytes=value)
                except Exception as exc:
                    result['error'] = bp.code(exc)
            stable = self.discover() == baseline
            report['discovery_stable'] = stable
            if not stable:
                for result in report['storage']:
                    result.update(status='unavailable', allocated_bytes=None, error='DISCOVERY_CHANGED_DURING_MEASUREMENT')
            complete = stable and all(x['status'] == 'measured' for x in report['storage'])
            report['result'] = 'passed' if complete else 'attention_required'
            report['measured_allocated_bytes_total'] = sum(x['allocated_bytes'] for x in report['storage']) if complete else None
        except Exception as exc:
            report['error'] = bp.code(exc)
            report['discovery_stable'] = None
            # An incomplete final verification cannot endorse earlier measurements.
            for result in report['storage']:
                result.update(status='unavailable', allocated_bytes=None, error='FINAL_IDENTITY_NOT_VERIFIED')
        report['docker_exec_commands'] = self.docker.exec_calls
        report['docker_metadata_commands'] = self.docker.calls + self.docker.base.calls
        report['finished_at'] = bp.stamp()
        report['scope_limits'] = ['no_file_contents_or_database_queries', 'live_data_may_change',
                                  'allocated_size_not_archive_or_logical_size', 'symlinks_not_followed_by_du',
                                  'no_destination_or_total_backup_capacity_decision',
                                  'client_timeout_does_not_prove_container_exec_exit_internal_timeout_is_separate']
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--langfuse-state-dir', type=Path, default=Path.home() / '.local/state/agent-project-langfuse')
    parser.add_argument('--max-seconds-per-store', type=int, default=30)
    args = parser.parse_args(argv)
    try:
        require(sys.platform == 'linux' and os.getuid() != 0, 'RUN_AS_ORIGINAL_NONROOT_USER_NO_SUDO')
        require(1 <= args.max_seconds_per_store <= 60, 'INVALID_DEADLINE')
        result = Measurement(args.langfuse_state_dir, seconds=args.max_seconds_per_store).run()
    except Exception as exc:
        result = {'schema': 1, 'stage': 'O04-observability-database-sizes', 'result': 'attention_required',
                  'error': bp.code(exc), 'backup_executed': False, 'restore_executed': False,
                  'service_control_requested': False, 'inference_requested': False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['result'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
