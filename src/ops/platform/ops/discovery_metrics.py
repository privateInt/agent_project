"""Infer metric roles from fresh samples, never from conventional job names."""
from datetime import datetime, timezone
import math
import re
import sys
from urllib.parse import urlencode

from src.infra.observability.ops.manage import scrape_timestamp
from src.ops.platform.contracts import ReadFailure, require
from src.ops.platform.observations import Prometheus
from src.ops.platform.resource_profiles import METRIC_UNITS, selector, validate_profile

SIGNATURES = {
    'host': ('node_cpu_seconds_total{mode="idle"}', 'node_memory_MemTotal_bytes',
             'node_memory_MemAvailable_bytes', 'node_filesystem_size_bytes', 'node_filesystem_avail_bytes'),
    'containers': ('container_cpu_usage_seconds_total{name!=""}',
                   'container_memory_working_set_bytes{name!=""}'),
    'gpu': tuple(METRIC_UNITS),
    'prometheus': ('prometheus_build_info',),
}
FS_EXCLUDE = 'tmpfs|devtmpfs|overlay|squashfs|nsfs|proc|sysfs|cgroup.*|debugfs|tracefs|securityfs|pstore|autofs|mqueue|hugetlbfs|fusectl|configfs|binfmt_misc|rpc_pipefs|efivarfs'


def probe_query(metric):
    return 'count by (job,instance,cluster) (' + metric + ' and (time() - timestamp(' + metric + ') < 90))'


def filesystem_query(host):
    metric = selector('node_filesystem_size_bytes', host, [('fstype', '!~', FS_EXCLUDE)])
    return 'count by (job,instance,cluster,mountpoint) (' + metric + ' and (time() - timestamp(' + metric + ') < 90))'


def clean(value):
    require(isinstance(value, str) and 0 < len(value) <= 256 and value.isprintable(), 'INVALID_METRIC_LABEL')
    return value


def identity(labels):
    require(isinstance(labels, dict), 'INVALID_METRIC_LABEL')
    return clean(labels.get('job')), clean(labels.get('instance')), clean(labels['cluster']) if labels.get('cluster') else ''


def vector(transport, expression):
    _, raw = transport.get('/api/v1/query?' + urlencode({'query': expression, 'timeout': '4s', 'limit': 257}))
    require(raw.get('status') == 'success' and isinstance(raw.get('data'), dict), 'INVALID_PROMETHEUS_RESPONSE')
    require(not raw.get('warnings') and not raw.get('infos'), 'INCOMPLETE_METRIC_RESPONSE')
    data = raw['data']
    require(data.get('resultType') == 'vector' and isinstance(data.get('result'), list), 'INVALID_PROMETHEUS_RESPONSE')
    require(len(data['result']) <= 256, 'DISCOVERY_SERIES_LIMIT')
    for row in data['result']:
        require(isinstance(row, dict) and isinstance(row.get('metric'), dict), 'INVALID_PROMETHEUS_RESPONSE')
        value = row.get('value')
        require(isinstance(value, list) and len(value) == 2 and type(value[0]) in (int, float)
                and math.isfinite(value[0]) and isinstance(value[1], str), 'INVALID_PROMETHEUS_RESPONSE')
        try: number = float(value[1])
        except ValueError: raise ReadFailure('INVALID_PROMETHEUS_RESPONSE') from None
        require(math.isfinite(number), 'UNAVAILABLE_METRIC_VALUE')
    return data['result']


def eligible_targets(targets, now):
    """Keep timestamp parsing independent of the host Python ISO parser.

    Reuse the existing RFC3339Nano parser. No infra command is invoked.
    Diagnostics retain only validated timestamps and selected public labels.
    """
    healthy, rows = set(), []
    exclusions = {key: 0 for key in ('not_up', 'invalid_timestamp', 'future_timestamp', 'stale_timestamp')}
    for target in targets:
        require(isinstance(target, dict), 'INVALID_PROMETHEUS_RESPONSE')
        ident = identity(target.get('labels'))
        health = target.get('health') if target.get('health') in ('up', 'down', 'unknown') else 'unknown'
        stamp, age = target.get('lastScrape'), None
        try:
            # The shared parser accepts explicit offsets and 0–9 fraction digits.
            # Missing/malformed timestamps stay invalid, never replaced with now.
            require(isinstance(stamp, str) and len(stamp) <= 40, 'INVALID_SCRAPE_TIMESTAMP')
            age = now.timestamp() - scrape_timestamp(stamp)
            state = 'future' if age < 0 else 'stale' if age >= 90 else 'fresh'
        except (ReadFailure, ValueError, TypeError, OverflowError, OSError):
            state, stamp = 'invalid', None
        reasons = []
        if health != 'up': reasons.append('not_up')
        if state != 'fresh': reasons.append(state + '_timestamp')
        for reason in reasons: exclusions[reason] += 1
        if not reasons: healthy.add(ident)
        rows.append({'job': ident[0], 'instance': ident[1], 'cluster': ident[2] or None,
                     'health': health, 'last_scrape': stamp,
                     'age_seconds': round(age, 6) if age is not None else None,
                     'timestamp_status': state, 'eligible': not reasons, 'exclusion_reasons': reasons})
    return healthy, {'schema': 1, 'clock': 'local_utc', 'freshness_seconds': 90,
                     'evaluated_at': now.isoformat().replace('+00:00', 'Z'),
                     'python_version': '.'.join(map(str, sys.version_info[:3])),
                     'health_up_count': sum(row['health'] == 'up' for row in rows),
                     'exclusions': exclusions, 'targets': rows}


def discover_metrics(transport, explicit=None, now=None, environment='docker-compose'):
    require(environment in ('docker-compose', 'kubernetes'), 'UNSUPPORTED_RESOURCE_ENVIRONMENT')
    _, raw = transport.get('/api/v1/targets?state=active')
    now = now or datetime.now(timezone.utc)
    require(raw.get('status') == 'success' and isinstance(raw.get('data'), dict), 'INVALID_PROMETHEUS_RESPONSE')
    targets = raw['data'].get('activeTargets')
    require(isinstance(targets, list) and len(targets) <= 256, 'DISCOVERY_TARGET_LIMIT')
    healthy, diagnostics = eligible_targets(targets, now)
    report = {'status': 'attention_required', 'active_target_count': len(targets),
              'fresh_healthy_target_count': len(healthy), 'roles': {}, 'generated_profile': None,
              'profile_validation': {}, 'explicit_profile_preserved': explicit is not None,
              'target_diagnostics': diagnostics}
    if explicit is not None:
        explicit = validate_profile(explicit)
        require(explicit['environment'] == environment, 'CONFIG_ENVIRONMENT_MISMATCH')
    selected = {}
    selected_cluster = explicit.get('scope', {}).get('cluster') if explicit else None
    signatures = dict(SIGNATURES)
    if environment == 'kubernetes':
        signatures['containers'] = (
            'container_cpu_usage_seconds_total{container!="",container!="POD",pod!="",namespace!=""}',
            'container_memory_working_set_bytes{container!="",container!="POD",pod!="",namespace!=""}')
    for role, metrics in signatures.items():
        try:
            sets = [{identity(row['metric']) for row in vector(transport, probe_query(metric))} & healthy
                    for metric in metrics]
            candidates = set.intersection(*sets)
            if selected_cluster is not None:
                candidates = {x for x in candidates if x[2] == selected_cluster}
            jobs = sorted({(x[0], x[2]) for x in candidates})
            report['roles'][role] = {'status': 'not_observed' if not jobs else 'ambiguous' if len(jobs) > 1 else 'verified',
                                     'candidates': [{'job': j, 'cluster': c or None} for j, c in jobs],
                                     'matched_target_count': len(candidates), 'signature_count': len(metrics)}
            if len(jobs) == 1:
                selected[role] = jobs[0]
        except ReadFailure as exc:
            report['roles'][role] = {'status': 'unavailable', 'error': exc.code}
    if not all(role in selected for role in ('host', 'containers', 'gpu')):
        report['reason'] = 'REQUIRED_METRIC_ROLE_NOT_UNIQUE_OR_UNAVAILABLE'
        return report
    clusters = {c for j, c in selected.values()}
    if len(clusters) != 1:
        report['reason'] = 'METRIC_CLUSTER_AMBIGUOUS'
        return report
    cluster = next(iter(clusters))
    scope = {'cluster': cluster} if cluster else {}
    host = {**scope, 'job': selected['host'][0]}
    mounts = sorted({clean(row['metric'].get('mountpoint')) for row in vector(transport, filesystem_query(host))
                     if identity(row['metric']) in healthy})
    require(mounts, 'FILESYSTEMS_NOT_OBSERVED')
    generated = validate_profile({'schema': 1, 'environment': environment, 'default_cluster': cluster or None,
        'scope': scope, 'sources': {role: {'job': selected[role][0]} for role in ('host', 'containers', 'gpu')},
        'target_jobs': sorted({job for job, c in selected.values()}), 'container_name_label': 'container' if environment == 'kubernetes' else 'name',
        'filesystems': {'mountpoint_include': '|'.join(re.escape(m) for m in mounts), 'fstype_exclude': FS_EXCLUDE}})
    report.update(generated_profile=generated, observed_mountpoints=mounts)
    # Evaluate the actual seven Ops queries, including rate windows and units.
    # The success cache is memory-only; production cache timestamps stay untouched.
    prom = Prometheus(transport.url.geturl(), {}, timeout=transport.timeout, resource_profile=generated)
    validation = {}
    for key in prom.queries:
        result = prom.metric(key)
        data = result.get('data') or {}
        validation[key] = {'status': result['status'], 'count': data.get('count'),
                           'coverage': data.get('coverage'), 'error': result.get('error')}
        if result['status'] == 'ok' and (not data.get('count') or any(r['value'] is None for r in data['items'])):
            validation[key]['status'] = 'not_observed'
        if environment == 'kubernetes' and result['status'] == 'ok' and (data.get('coverage') != 'query_result'
                or len(data.get('items', [])) != data.get('count')
                or key == 'targets' and any(row['value'] != 1 for row in data.get('items', []))):
            validation[key]['status'] = 'not_ready'
    report['profile_validation'] = validation
    report['status'] = 'verified' if all(v['status'] == 'ok' for v in validation.values()) else 'attention_required'
    return report
