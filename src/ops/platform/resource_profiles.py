"""Validated deployment selectors and common resource identities for Prometheus.

An administrator supplies selectors in server config. Browser requests never
choose upstream URLs, queries, profiles or credentials. Discovery belongs to
the collector deployment, not the Ops UI.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from .contracts import require
from .transport import strict_json

PROFILE_ROOT = Path(__file__).with_name('profiles')
LABEL = re.compile(r'[a-zA-Z_][a-zA-Z0-9_]*')
IDENTITY_FIELDS = ('cluster', 'node', 'namespace', 'workload', 'workload_kind', 'pod', 'container')
PUBLIC_LABELS = frozenset((*IDENTITY_FIELDS, 'job', 'instance', 'name', 'device', 'mountpoint',
                          'UUID', 'gpu', 'modelName', '__name__', 'endpoint', 'service', 'metrics_path'))
METRIC_UNITS = {'DCGM_FI_DEV_GPU_UTIL': '%', 'DCGM_FI_DEV_FB_TOTAL': 'MiB', 'DCGM_FI_DEV_FB_USED': 'MiB',
                'DCGM_FI_DEV_FB_FREE': 'MiB', 'DCGM_FI_DEV_GPU_TEMP': '°C', 'DCGM_FI_DEV_POWER_USAGE': 'W'}
HEALTH_KEY = 'cluster,job,instance'
HOST_KEY = 'cluster,job,instance,node'


def text(value, limit=256):
    require(isinstance(value, str) and 0 < len(value) <= limit and value.isprintable(), 'INVALID_RESOURCE_PROFILE')
    return value


def validate_profile(value):
    keys = {'schema', 'environment', 'default_cluster', 'scope', 'sources', 'target_jobs',
            'container_name_label', 'filesystems'}
    require(isinstance(value, dict) and set(value) == keys and type(value['schema']) is int and
            value['schema'] == 1, 'INVALID_RESOURCE_PROFILE')
    require(value['environment'] in ('docker-compose', 'kubernetes'), 'UNSUPPORTED_RESOURCE_ENVIRONMENT')
    if value['default_cluster'] is not None:
        text(value['default_cluster'])
    require(isinstance(value['scope'], dict) and set(value['scope']) <= {'cluster'}, 'INVALID_RESOURCE_SCOPE')
    for v in value['scope'].values(): text(v)
    require(not value['scope'].get('cluster') or value['default_cluster'] in (None, value['scope']['cluster']), 'RESOURCE_CLUSTER_MISMATCH')
    require(isinstance(value['sources'], dict) and set(value['sources']) == {'host', 'containers', 'gpu'}, 'INVALID_RESOURCE_PROFILE')
    reserved = {'host': {'mode', 'fstype', 'mountpoint'}, 'containers': {'name', 'container', 'pod'}, 'gpu': set()}
    for role, selectors in value['sources'].items():
        require(isinstance(selectors, dict) and 'job' in selectors and 1 <= len(selectors) <= 8, 'INVALID_RESOURCE_PROFILE')
        for k, v in selectors.items():
            require(isinstance(k, str) and LABEL.fullmatch(k) and not k.startswith('__') and
                    k != 'cluster' and k not in reserved[role], 'INVALID_RESOURCE_LABEL')
            text(v)
    jobs = value['target_jobs']
    require(isinstance(jobs, list) and 1 <= len(jobs) <= 16 and all(isinstance(j, str) for j in jobs) and
            len(set(jobs)) == len(jobs), 'INVALID_RESOURCE_PROFILE')
    for job in jobs: text(job)
    require(value['container_name_label'] in ('name', 'container'), 'INVALID_RESOURCE_PROFILE')
    require(value['environment'] != 'kubernetes' or value['container_name_label'] == 'container', 'INVALID_RESOURCE_PROFILE')
    require(isinstance(value['filesystems'], dict) and set(value['filesystems']) == {'mountpoint_include', 'fstype_exclude'}, 'INVALID_RESOURCE_PROFILE')
    for pattern in value['filesystems'].values():
        text(pattern, 512)
        # Conservative common Python/RE2 subset. Native Prometheus validation is
        # still required for a newly supplied deployment profile.
        require('(?' not in pattern and not re.search(r'\\[1-9]', pattern), 'UNSUPPORTED_RESOURCE_REGEX')
        try: re.compile(pattern)
        except re.error: require(False, 'INVALID_RESOURCE_REGEX')
    return deepcopy(value)


def profile(value=None):
    if value is None:
        value = strict_json((PROFILE_ROOT / 'docker-compose.json').read_bytes())
    return validate_profile(value)


def selector(metric, values, extra=()):
    parts = [k + '=' + json.dumps(v, ensure_ascii=False) for k, v in values.items()]
    parts.extend(k + op + json.dumps(v, ensure_ascii=False) for k, op, v in extra)
    return metric + '{' + ','.join(parts) + '}'


def queries(config):
    """Same bounded metric catalogue across environments, deployment selectors vary."""
    config = validate_profile(config)
    def labels(role): return {**config['sources'][role], **config['scope']}
    def metric(name, role, extra=()): return selector(name, labels(role), extra)
    def up(role): return metric('up', role)
    def fresh(expression, role):
        return '(' + expression + ') and (time() - timestamp(' + expression + ') < 90) and on(' + HEALTH_KEY + ') (' + up(role) + ' == 1)'
    cpu = metric('node_cpu_seconds_total', 'host', [('mode', '=', 'idle')])
    total = metric('node_memory_MemTotal_bytes', 'host')
    avail = metric('node_memory_MemAvailable_bytes', 'host')
    fs_extra = [('fstype', '!~', config['filesystems']['fstype_exclude']),
                ('mountpoint', '=~', config['filesystems']['mountpoint_include'])]
    fs = metric('node_filesystem_size_bytes', 'host', fs_extra)
    fsa = metric('node_filesystem_avail_bytes', 'host', fs_extra)
    container_extra = [(config['container_name_label'], '!=', '')]
    if config['environment'] == 'kubernetes':
        container_extra += [('container', '!=', 'POD'), ('pod', '!=', '')]
    ccpu = metric('container_cpu_usage_seconds_total', 'containers', container_extra)
    cmem = metric('container_memory_working_set_bytes', 'containers', container_extra)
    targets = selector('up', config['scope'], [('job', '=~', '|'.join(re.escape(j) for j in config['target_jobs']))])
    # Check the original sample timestamp per metric, then copy its name to a
    # temporary label so `or` preserves every field and deployment label.
    # Copying before timestamp() would replace the original source timestamp.
    gpu = ' or '.join('label_replace((' + fresh(metric(name, 'gpu'), 'gpu') +
        '), "ops_metric_name", "$1", "__name__", "(.+)")' for name in METRIC_UNITS)
    return {
        'targets': ('수집 연결', 'state', targets + ' and (time() - timestamp(' + targets + ') < 90)'),
        'host_cpu': ('호스트 CPU 사용률', '%', '(100 * (1 - avg by(' + HOST_KEY + ')(rate(' + cpu + '[5m])))) and on(' + HEALTH_KEY + ') (time() - max by(' + HEALTH_KEY + ')(timestamp(' + cpu + ')) < 90) and on(' + HEALTH_KEY + ') (' + up('host') + ' == 1)'),
        'host_memory': ('호스트 메모리 사용률', '%', '(100 * (1 - ' + avail + ' / ' + total + ')) and (' + fresh(total, 'host') + ') and (' + fresh(avail, 'host') + ')'),
        'filesystems': ('디스크 사용률', '%', '(100 * (1 - ' + fsa + ' / ' + fs + ')) and (' + fresh(fs, 'host') + ') and (' + fresh(fsa, 'host') + ')'),
        'container_cpu': ('컨테이너 CPU', 'cores', 'rate(' + ccpu + '[5m]) and (' + fresh(ccpu, 'containers') + ')'),
        'container_memory': ('컨테이너 메모리', 'bytes', fresh(cmem, 'containers')),
        'gpu': ('GPU', 'source_metric', gpu),
    }


def resource_identity(key, labels, config):
    """Observed labels are authoritative; absent metadata stays unknown."""
    identity = {k: labels.get(k) or None for k in IDENTITY_FIELDS}
    identity['cluster'] = identity['cluster'] or config['default_cluster'] or config['scope'].get('cluster')
    if config['environment'] == 'docker-compose' and not identity['container'] and key.startswith('container_'):
        identity['container'] = labels.get('name') or None
    identity.update(environment=config['environment'], instance=labels.get('instance'),
                    kind={'host_cpu': 'node', 'host_memory': 'node', 'targets': 'scrape_target',
                          'filesystems': 'filesystem', 'gpu': 'gpu'}.get(key, 'container'),
                    mountpoint=labels.get('mountpoint'), device=labels.get('UUID') or labels.get('device') or labels.get('gpu'))
    identity['id'] = hashlib.sha256(json.dumps({'metric': key, 'identity': identity, 'labels': labels},
                                              sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    identity['metadata_complete'] = bool(identity['cluster'] and identity['node'] and
        (not key.startswith('container_') or (identity['container'] and
         (config['environment'] != 'kubernetes' or identity['namespace'] and identity['pod']))))
    return identity


DEFAULT_PROFILE = profile()
DEFAULT_QUERIES = queries(DEFAULT_PROFILE)
