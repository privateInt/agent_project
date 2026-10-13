"""Reconcile only values explicitly owned by accepted discovery provenance."""
from copy import deepcopy

from src.ops.platform.contracts import require
from src.ops.platform.ops.activation_plan import PLATFORMS, OBSERVATIONS
from src.ops.platform.resource_profiles import validate_profile
from src.ops.platform.transport import base_url

URLS = {**{'platforms.' + p + '.url': p for p in PLATFORMS}, **{p + '_url': p for p in OBSERVATIONS}}
FIELDS = frozenset((*URLS, 'resource_profile'))


def value(cfg, key):
    return cfg['platforms'][URLS[key]]['url'] if key.startswith('platforms.') else cfg.get(key)


def put(cfg, key, item):
    if key.startswith('platforms.'): cfg['platforms'][URLS[key]]['url'] = item
    else: cfg[key] = deepcopy(item)


def merge(cfg, discovery, previous):
    require(discovery.get('result') == 'discovered' and discovery.get('deployment_metadata_stable') is True,
            'VERIFIED_DISCOVERY_REQUIRED')
    require(isinstance(previous, dict) and set(previous) == FIELDS, 'PROVENANCE_FIELDS_INVALID')
    require(discovery.get('provider', 'docker-compose') == 'docker-compose', 'KUBERNETES_REFRESH_NOT_IMPLEMENTED')
    desired, fields, before_fields = deepcopy(cfg), {}, {}
    changes, overrides, differences = [], [], []
    for key in (*URLS, 'resource_profile'):
        old = previous[key]; current = value(cfg, key)
        require(current is not None, 'PROVENANCE_FIELD_REMOVED')
        require(old.get('source') in ('discovery', 'existing_preserved', 'user_override') and
                type(old.get('automatic_future_update_allowed')) is bool, 'PROVENANCE_FIELDS_INVALID')
        owned = old['source'] == 'discovery' and old['automatic_future_update_allowed'] is True
        edited = current != old['value']
        if edited: overrides.append(key)
        owned = owned and not edited
        field = deepcopy(old)
        field.update(source='discovery' if owned else 'user_override' if edited else old['source'],
                     value=deepcopy(current), automatic_future_update_allowed=owned)
        # Rollback restores current user settings and must retain ownership releases.
        before_fields[key] = deepcopy(field)
        if key in URLS:
            role = URLS[key]; item = discovery['services'][role]
            candidate = discovery['connection_overlay']['platforms'][role]['url'] if role in PLATFORMS else discovery['connection_overlay'][key]
            base_url(candidate); base_url(current)
            if owned: require(old.get('identity') == item['identity'], 'DISCOVERY_LOGICAL_IDENTITY_CHANGED')
            field.update(discovered_value=candidate, observed_identity=item['identity'])
            if owned: put(desired, key, candidate)
            field['matches_discovery'] = value(desired, key).rstrip('/') == candidate.rstrip('/')
            if not field['matches_discovery']: differences.append(key)
        else:
            candidate = validate_profile(discovery['metrics']['generated_profile'])
            effective = validate_profile(current)
            require(candidate['environment'] == effective['environment'] == 'docker-compose', 'KUBERNETES_REFRESH_NOT_IMPLEMENTED')
            if owned:
                require(old.get('prometheus_identity') == discovery['services']['prometheus']['identity'], 'DISCOVERY_LOGICAL_IDENTITY_CHANGED')
                require(fields['prometheus_url']['matches_discovery'], 'PROFILE_SOURCE_CONFLICT')
                require(candidate['scope'] == effective['scope'] and candidate['default_cluster'] == effective['default_cluster'],
                        'RESOURCE_SCOPE_CHANGED')
                put(desired, key, candidate)
            field['matches_discovery'] = value(desired, key) == candidate
            if not field['matches_discovery']: differences.append(key)
        field['value'] = deepcopy(value(desired, key)); fields[key] = field
        if field['value'] != current: changes.append(key)
    return desired, {'schema': 1, 'provider': 'docker-compose', 'fields': fields,
                     'rollback_fields': before_fields, 'changed_fields': changes, 'manual_overrides_detected': overrides,
                     'preserved_overrides_differing_from_discovery': differences,
                     'managed_fields': [key for key, field in fields.items() if field['automatic_future_update_allowed']],
                     # A coherent manual config edit may not have reached the running process.
                     # Reload it once alongside owned changes without rewriting its values.
                     'restart_required': bool(changes or overrides), 'unmanaged_values_preserved': True,
                     'discovery_checked_at': discovery.get('checked_at'), 'continuous_refresh_supported': True}
