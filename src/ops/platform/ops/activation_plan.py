"""Merge a fresh discovery result while preserving unclassified existing settings."""
from copy import deepcopy

from src.ops.platform.contracts import require
from src.ops.platform.observations import Prometheus
from src.ops.platform.ops.discover import health
from src.ops.platform.resource_profiles import profile, validate_profile
from src.ops.platform.transport import Transport, base_url

PLATFORMS = ('model', 'data', 'knowledge', 'agent')
OBSERVATIONS = ('prometheus', 'alertmanager', 'langfuse')


def merge(cfg, discovery):
    require(discovery.get('result') == 'discovered' and discovery.get('deployment_metadata_stable') is True,
            'VERIFIED_DISCOVERY_REQUIRED')
    overlay = discovery['connection_overlay']
    require(isinstance(overlay, dict), 'DISCOVERY_OVERLAY_REQUIRED')
    desired, fields, changes = deepcopy(cfg), {}, []
    for role in (*PLATFORMS, *OBSERVATIONS):
        key = 'platforms.' + role + '.url' if role in PLATFORMS else role + '_url'
        candidate = overlay['platforms'][role]['url'] if role in PLATFORMS else overlay[key]
        base_url(candidate)
        existing = cfg['platforms'][role]['url'] if role in PLATFORMS else cfg.get(key)
        item = {'source': 'existing_preserved' if existing is not None else 'discovery',
                'value': existing if existing is not None else candidate,
                'discovered_value': candidate, 'identity': discovery['services'][role]['identity'],
                'matches_discovery': existing is None or existing.rstrip('/') == candidate.rstrip('/'),
                'automatic_future_update_allowed': existing is None}
        fields[key] = item
        if existing is None:
            require(role in OBSERVATIONS, 'PLATFORM_CONFIGURATION_REQUIRED')
            desired[key] = candidate; changes.append(key)
    if 'resource_profile' in cfg:
        effective = validate_profile(cfg['resource_profile'])
        source = 'existing_preserved'
    else:
        # A discovered profile belongs to the discovered Prometheus. A manual
        # Prometheus override must not receive selectors from another server.
        require(fields['prometheus_url']['matches_discovery'], 'PROFILE_SOURCE_CONFLICT')
        effective = validate_profile(discovery['metrics']['generated_profile'])
        desired['resource_profile'] = effective
        changes.append('resource_profile'); source = 'discovery'
    fields['resource_profile'] = {'source': source, 'value': effective,
                                  'prometheus_identity': discovery['services']['prometheus']['identity'] if source == 'discovery' else None,
                                  'automatic_future_update_allowed': source == 'discovery'}
    require(effective['environment'] == 'docker-compose', 'KUBERNETES_ACTIVATION_NOT_IMPLEMENTED')
    return desired, {'schema': 1, 'provider': 'docker-compose', 'changed_fields': changes,
                     'discovery_checked_at': discovery.get('checked_at'),
                     'fields': fields, 'restart_required': bool(changes),
                     'existing_values_preserved': True, 'continuous_refresh_supported': False}


def validate_effective(cfg):
    """Check chosen overrides too, without sending domain API credentials."""
    for role in (*PLATFORMS, *OBSERVATIONS):
        url = cfg['platforms'][role]['url'] if role in PLATFORMS else cfg[role + '_url']
        health(role, Transport(url, timeout=cfg['timeout']))
    prom = Prometheus(cfg['prometheus_url'], {}, cfg['timeout'], profile(cfg.get('resource_profile')))
    result = {}
    for key in prom.queries:
        response = prom.metric(key); data = response.get('data') or {}
        require(response['status'] == 'ok' and data.get('count', 0) > 0
                and data.get('coverage') == 'query_result' and len(data.get('items', [])) == data['count']
                and all(row['value'] is not None for row in data['items'])
                and (key != 'targets' or all(row['value'] == 1 for row in data['items'])), 'EFFECTIVE_PROFILE_NOT_READY')
        result[key] = {'status': 'ok', 'count': data['count']}
    return result
