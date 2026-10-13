"""Current-user systemd adapter. Only one derived unit can be controlled."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import quote

from ops_platform.contracts import ReadFailure, require
from ops_platform.ops import lifecycle_probe as probe, manage

PROPERTIES = probe.UNIT_PROPERTIES + ',DropInPaths,ControlGroup,Job,NeedDaemonReload'
OPERATIONS = {'inspect_unit', 'unit_paths', 'daemon_reload', 'start', 'stop', 'reset_failed'}
OUTCOMES = {'nonzero_exit', 'timeout', 'unavailable', 'response_limit'}


def command_details(value):
    """Only fixed operation/outcome labels and an integer can leave this boundary."""
    if (not isinstance(value, dict) or not isinstance(value.get('operation'), str)
            or not isinstance(value.get('outcome'), str)
            or value['operation'] not in OPERATIONS or value['outcome'] not in OUTCOMES):
        return None
    result = {key: value[key] for key in ('operation', 'outcome')}
    if type(value.get('returncode')) is int and -128 <= value['returncode'] <= 255:
        result['returncode'] = value['returncode']
    return result


class CommandFailure(ReadFailure):
    def __init__(self, code, operation, outcome, returncode=None):
        super().__init__(code)
        self.service_command = command_details({'operation': operation, 'outcome': outcome, 'returncode': returncode})


def quoted(value, expand_dollar=False):
    require(isinstance(value, str) and not any(ord(c) < 32 or ord(c) == 127 for c in value), 'INVALID_UNIT_ARGUMENT')
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if not expand_dollar: value = value.replace('$', '$$')
    return '"' + value + '"'


def render(manifest):
    # env -i prevents inherited manager credentials/proxies/Python paths from
    # becoming the worker environment. No shell or environment file is used.
    args = [manifest['env_executable'], '-i']
    args += [key + '=' + value for key, value in sorted(manifest['environment'].items())]
    command = ' '.join(quoted(arg) for arg in args)
    command += ' ' + quoted('INVOCATION_ID=${INVOCATION_ID}', expand_dollar=True)
    command += ' ' + ' '.join(quoted(arg) for arg in [manifest['binding']['python'], '-I', '-B',
        str(Path(manifest['binding']['owner']['project_root']) / 'ops_platform/ops/service_entry.py'),
        '--state-dir', manifest['binding']['directory']])
    return ('[Unit]\nDescription=Agent Project discovered Ops refresh\n'
            'StartLimitIntervalSec=300\nStartLimitBurst=3\n\n[Service]\nType=exec\n'
            'ExecStart=' + command + '\nRestart=on-failure\nRestartPreventExitStatus=2\n'
            'RestartSec=30\nKillMode=mixed\nSendSIGKILL=yes\nTimeoutStopSec=' + str(manifest['deadline_seconds'] + 20) +
            '\nUMask=0077\nStandardOutput=journal\nStandardError=journal\n\n[Install]\nWantedBy=default.target\n')


class Systemd:
    def __init__(self, reader, report, unit):
        require(probe.UNIT.fullmatch(unit) is not None, 'INVALID_MANAGEMENT_UNIT')
        require(report.get('status') == 'ok', 'SYSTEMD_USER_MANAGER_UNAVAILABLE')
        self.unit, self.reader = unit, reader
        self.environment = dict(reader.env)
        runtime = report.get('environment', {}).get('selected_runtime')
        if runtime:
            runtime = probe.validate_runtime(runtime)
            self.environment.update(XDG_RUNTIME_DIR=runtime, DBUS_SESSION_BUS_ADDRESS='unix:path=' + quote(runtime + '/bus', safe='/-_.'))
        self.binary = reader.paths['systemctl']
        self.analyze = shutil.which('systemd-analyze', path=reader.original.get('PATH', os.defpath))
        self.env_binary = shutil.which('env', path=reader.original.get('PATH', os.defpath))
        self.control_commands = 0

    def call(self, args, control=False, accept=()):
        readonly = args in (['show', '--property=' + PROPERTIES, self.unit], ['show', '--property=UnitPath'])
        mutation = args in (['daemon-reload'], ['--no-block', 'start', self.unit],
                           ['--no-block', 'stop', self.unit], ['reset-failed', self.unit])
        require(readonly and not control or mutation and control, 'SERVICE_COMMAND_REFUSED')
        operation = ('inspect_unit' if len(args) == 3 else 'unit_paths') if readonly else (
            args[1] if args[0] == '--no-block' else args[0].replace('-', '_'))
        if control: self.control_commands += 1
        self.reader.calls += 1
        try:
            result = self.reader.runner([self.binary, '--user', '--no-pager', '--no-ask-password', *args],
                env=self.environment, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=False)
        except subprocess.TimeoutExpired:
            raise CommandFailure('SERVICE_COMMAND_OUTCOME_UNKNOWN' if control else 'SERVICE_QUERY_TIMEOUT', operation, 'timeout') from None
        except OSError: raise CommandFailure('SERVICE_COMMAND_UNAVAILABLE', operation, 'unavailable') from None
        if result.returncode != 0 and result.returncode not in accept:
            raise CommandFailure('SERVICE_COMMAND_FAILED' if control else 'SERVICE_QUERY_FAILED', operation, 'nonzero_exit', result.returncode)
        if len(result.stdout) >= 131072: raise CommandFailure('SERVICE_RESPONSE_TOO_LARGE', operation, 'response_limit')
        return result.stdout.strip()

    def inspect(self):
        value = probe.properties(self.call(['show', '--property=' + PROPERTIES, self.unit], accept=(1, 4)), set(PROPERTIES.split(',')))
        require(value.get('Id') == self.unit and value.get('LoadState') in ('not-found', 'loaded'), 'SERVICE_LOAD_STATE_INVALID')
        require(value.get('ActiveState') in ('inactive', 'active', 'activating', 'deactivating', 'failed'), 'SERVICE_ACTIVE_STATE_INVALID')
        require(re.fullmatch('[0-9]+', value.get('MainPID', '')) is not None, 'SERVICE_PID_INVALID')
        require(not value.get('InvocationID') or re.fullmatch('[0-9a-f]{32}', value['InvocationID']), 'SERVICE_INVOCATION_INVALID')
        return value

    def unit_paths(self):
        raw = probe.properties(self.call(['show', '--property=UnitPath']), {'UnitPath'}).get('UnitPath', '')
        # systemctl escapes spaces in string arrays; decode its \xNN encoding,
        # never split an escaped path into two paths or evaluate shell text.
        return [re.sub(r'\\x([0-9a-fA-F]{2})', lambda m: chr(int(m[1], 16)), item) for item in raw.split()]

    def verify(self, manifest):
        require(self.analyze and self.env_binary, 'SERVICE_VALIDATOR_NOT_INSTALLED')
        with tempfile.TemporaryDirectory(prefix='ops-unit-verify-') as temporary:
            path = Path(temporary) / self.unit
            path.write_text(render(manifest)); path.chmod(0o600)
            try:
                result = self.reader.runner([self.analyze, '--user', '--man=no', 'verify', str(path)],
                    env=self.environment, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15, check=False)
            except (subprocess.TimeoutExpired, OSError): raise ReadFailure('SERVICE_UNIT_VALIDATION_UNAVAILABLE') from None
            require(result.returncode == 0, 'SERVICE_UNIT_VALIDATION_FAILED')

    def reload(self): self.call(['daemon-reload'], control=True)
    def start(self): self.call(['--no-block', 'start', self.unit], control=True)
    def stop(self): self.call(['--no-block', 'stop', self.unit], control=True)
    def reset_failed(self): self.call(['reset-failed', self.unit], control=True)

    def cgroup_empty(self, state):
        group = state.get('ControlGroup', '')
        if not group: return True
        require(group.startswith('/') and '..' not in Path(group).parts, 'INVALID_SERVICE_CGROUP')
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            left, sep, right = line.partition(' - ')
            if not sep or right.split()[0] != 'cgroup2': continue
            fields = left.split()
            unescape = lambda s: re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), s)
            root, mount = Path(unescape(fields[3])), Path(unescape(fields[4]))
            try: relative = Path(group).relative_to(root)
            except ValueError: continue
            path = mount / relative / 'cgroup.events'
            try: values = dict(row.split() for row in path.read_text().splitlines())
            except FileNotFoundError: return True
            return values.get('populated') == '0'
        return False  # An unsupported hierarchy is not evidence that children exited.
