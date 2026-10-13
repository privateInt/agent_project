"""Host-only Compose adapter. Project membership precedes endpoint probing."""
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess

from src.infra.observability.ops.backup_preflight import DockerReadOnly, IDENT, Unavailable
from src.ops.platform.contracts import require
from src.ops.platform.transport import base_url

# Logical service contracts; project names, container names and host ports vary.
ROLES = {
    'model': ('gateway', 'src/platform/model'),
    'data': ('data-gateway', 'src/platform/data'),
    'knowledge': ('knowledge-gateway', 'src/platform/knowledge'),
    'agent': ('agent-gateway', 'src/platform/agent'),
    'prometheus': ('obs-prometheus', 'src/infra/observability'),
    'alertmanager': ('obs-alertmanager', 'src/infra/observability'),
    'langfuse': ('lf-web', 'src/infra/langfuse'),
}
FIELDS = ('id', 'image', 'state', 'started_at', 'project', 'service', 'working_dir',
          'config_files', 'network_mode', 'ports', 'listen_flags')
_fields = ['.Id', '.Image', '.State.Status', '.State.StartedAt'] + [
    '(index .Config.Labels "com.docker.compose.' + label + '")' for label in
    ('project', 'service', 'project.working_dir', 'project.config_files')
] + ['.HostConfig.NetworkMode', '.NetworkSettings.Ports']
# Project only the listen flag, never the full command, environment or labels.
LISTEN_PREFIX = '--web.listen-address='
FORMAT = '[' + ','.join('{{json ' + field + '}}' for field in _fields) + \
    ',[{{range .Config.Cmd}}{{if ge (len .) 21}}{{if eq (slice . 0 21) "--web.listen-address="}}{{json .}},{{end}}{{end}}{{end}}null]]'


class DockerDiscovery(DockerReadOnly):
    def _command(self, args, bootstrap=False):
        listing = (len(args) == 5 and args[:4] == ['ps', '-aq', '--no-trunc', '--filter']
                   and args[4] in {'label=com.docker.compose.service=' + v[0] for v in ROLES.values()})
        inspecting = (len(args) == 5 and args[:3] == ['container', 'inspect', '--format']
                      and args[3] == FORMAT and IDENT.fullmatch(args[4]))
        if not (listing or inspecting):
            return super()._command(args, bootstrap)
        require(self.endpoint is not None and not bootstrap, 'LOCAL_DOCKER_NOT_VERIFIED')
        self.calls += 1
        try:
            result = subprocess.run(['docker', '--host', self.endpoint, *args], capture_output=True,
                                    text=True, env=self.env, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise Unavailable('DOCKER_READ_UNAVAILABLE') from None
        require(result.returncode == 0 and len(result.stdout) <= 4 * 1024 * 1024, 'DOCKER_READ_FAILED')
        return result.stdout.strip()

    def inventory(self):
        rows = []
        for service in sorted({v[0] for v in ROLES.values()}):
            ids = self._command(['ps', '-aq', '--no-trunc', '--filter',
                                 'label=com.docker.compose.service=' + service]).split()
            require(len(ids) <= 16 and all(IDENT.fullmatch(i) for i in ids), 'CONTAINER_LIMIT_OR_ID_INVALID')
            for ident in ids:
                values = json.loads(self._command(['container', 'inspect', '--format', FORMAT, ident]))
                require(isinstance(values, list) and len(values) == len(FIELDS), 'INVALID_DOCKER_METADATA')
                row = dict(zip(FIELDS, values))
                require(row['id'] == ident and row['service'] == service, 'DOCKER_IDENTITY_CHANGED')
                rows.append(row)
        return rows


def belongs(row, root, role):
    service, subtree = ROLES[role]
    if row.get('service') != service:
        return False
    working = row.get('working_dir')
    files = row.get('config_files')
    if not isinstance(working, str) or not isinstance(files, str) or not files:
        return False
    folder = root / subtree
    work = Path(working)
    if not work.is_absolute():
        return False
    paths = [Path(p) if Path(p).is_absolute() else work / p for p in files.split(',')]
    # Use normalized lexical paths: no probing other checkouts or resolving their links.
    return bool(row.get('project')) and all(
        Path(os.path.normpath(p)).is_relative_to(folder) for p in paths)


def scoped(rows, root):
    return sorted((row for row in rows if any(belongs(row, root, role) for role in ROLES)),
                  key=lambda row: row['id'])


def http_endpoint(host, port):
    require(isinstance(host, str) and str(port).isascii() and str(port).isdigit(), 'INVALID_PORT_BINDING')
    port = int(port)
    require(1 <= port <= 65535, 'INVALID_PORT_BINDING')
    try:
        addr = ipaddress.ip_address(host or '0.0.0.0')
    except ValueError:
        require(False, 'IP_LISTEN_ADDRESS_REQUIRED')
    require(not addr.is_multicast, 'INVALID_PORT_BINDING')
    # A wildcard bind is reachable locally; never attempt to connect to 0.0.0.0.
    host = '127.0.0.1' if addr.is_unspecified else str(addr)
    host = '[' + host + ']' if ':' in host else host
    result = 'http://' + host + ':' + str(port)
    base_url(result)
    return result


def endpoints(row, role):
    candidates = set()
    if row.get('network_mode') == 'host':
        require(role in ('prometheus', 'alertmanager'), 'HOST_NETWORK_ENDPOINT_UNSUPPORTED')
        flags = [x for x in row.get('listen_flags', []) if x is not None]
        require(len(flags) == 1, 'LISTEN_FLAG_MISSING_OR_AMBIGUOUS')
        match = re.fullmatch(r'--web.listen-address=(\[[0-9a-fA-F:]+\]|[^:]*):([0-9]+)', flags[0])
        require(match is not None, 'INVALID_LISTEN_FLAG')
        candidates.add(http_endpoint(match[1].strip('[]'), match[2]))
        source = 'compose_service_and_listen_flag'
    else:
        require(isinstance(row.get('ports'), dict), 'PUBLISHED_PORT_MISSING')
        for container_port, bindings in row['ports'].items():
            if not container_port.endswith('/tcp'):
                continue
            for binding in bindings or []:
                candidates.add(http_endpoint(binding.get('HostIp'), binding.get('HostPort')))
        source = 'compose_service_and_published_port'
    require(len(candidates) == 1, 'ENDPOINT_MISSING_OR_AMBIGUOUS')
    return next(iter(candidates)), source
