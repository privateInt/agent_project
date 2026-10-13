"""Read-only Kubernetes service/metric proposals scoped by labels and object UIDs."""
from copy import deepcopy
from datetime import datetime, timezone
import ipaddress
import re

from ops_platform.contracts import ReadFailure, require
from ops_platform.ops.discovery_metrics import discover_metrics
from ops_platform.ops.kubernetes_client import Kubectl, PART_OF, INSTALLATION, ROLE, PORT, name, label, digest
from ops_platform.transport import Transport

ROLES = ('model', 'data', 'knowledge', 'agent', 'prometheus', 'alertmanager', 'langfuse')
PLATFORMS = ROLES[:4]


def uid(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,127}', value), 'INVALID_KUBERNETES_UID')
    return value


def metadata(row, namespace=None):
    meta = row.get('metadata', {})
    require(isinstance(meta, dict) and not meta.get('deletionTimestamp'), 'KUBERNETES_OBJECT_DELETING')
    result = {'name': name(meta.get('name')), 'uid': uid(meta.get('uid'))}
    if namespace is not None:
        require(meta.get('namespace') == namespace, 'KUBERNETES_NAMESPACE_MISMATCH')
        result['namespace'] = name(namespace)
    return result


def project_service(row):
    meta = row.get('metadata', {}); labels = meta.get('labels', {}); annotations = meta.get('annotations', {})
    require(isinstance(labels, dict) and labels.get(PART_OF) == 'agent-project', 'KUBERNETES_SERVICE_SCOPE_MISMATCH')
    require(labels.get(ROLE) in ROLES and bool(labels.get(INSTALLATION)), 'KUBERNETES_SERVICE_LABELS_REQUIRED')
    namespace = name(meta.get('namespace')); result = metadata(row, namespace)
    require(row.get('kind') == 'Service' and row.get('apiVersion') == 'v1', 'INVALID_KUBERNETES_RESPONSE')
    spec = row.get('spec', {})
    ports = spec.get('ports', [])
    require(isinstance(ports, list) and len(ports) <= 32, 'INVALID_SERVICE_PORTS')
    selector = spec.get('selector')
    require(selector is None or isinstance(selector, dict), 'INVALID_SERVICE_SELECTOR')
    result.update(role=labels[ROLE], installation=label(labels[INSTALLATION]),
                  service_type=spec.get('type', 'ClusterIP'), cluster_ip=spec.get('clusterIP'),
                  publish_not_ready=spec.get('publishNotReadyAddresses', False), selector=deepcopy(selector),
                  port_hint=annotations.get(PORT), ports=[{k:p[k] for k in
                     ('name','port','targetPort','protocol','appProtocol') if k in p} for p in ports])
    return result


def slice_projection(row, service):
    ns, service_name = service['namespace'], service['name']
    require(row.get('kind') == 'EndpointSlice' and row.get('apiVersion') == 'discovery.k8s.io/v1', 'INVALID_KUBERNETES_RESPONSE')
    result = metadata(row, ns); meta = row['metadata']
    require(meta.get('labels', {}).get('kubernetes.io/service-name') == service_name, 'ENDPOINTSLICE_SERVICE_MISMATCH')
    owners = [r for r in meta.get('ownerReferences', []) if r.get('kind') == 'Service']
    require(len(owners) == 1 and owners[0].get('apiVersion') == 'v1' and owners[0].get('uid') == service['uid']
            and owners[0].get('name') == service_name, 'ENDPOINTSLICE_OWNER_MISMATCH')
    ports, endpoints = row.get('ports', []), row.get('endpoints', [])
    require(isinstance(ports, list) and len(ports) <= 32 and isinstance(endpoints, list) and len(endpoints) <= 1024,
            'ENDPOINTSLICE_SIZE_LIMIT')
    result.update(address_type=row.get('addressType'), ports=[{k:p[k] for k in ('name','port','protocol') if k in p} for p in ports],
                  endpoints=[])
    for endpoint in endpoints:
        require(isinstance(endpoint, dict) and isinstance(endpoint.get('addresses'), list) and len(endpoint['addresses']) <= 8,
                'INVALID_ENDPOINTSLICE')
        conditions = endpoint.get('conditions') or {}; ref = endpoint.get('targetRef') or {}
        require(isinstance(conditions, dict) and isinstance(ref, dict), 'INVALID_ENDPOINTSLICE')
        result['endpoints'].append({'addresses': sorted(endpoint['addresses']),
            'conditions': {k:conditions[k] for k in ('ready','serving','terminating') if k in conditions},
            'target': {k:ref[k] for k in ('kind','namespace','name','uid') if k in ref}})
    result['endpoints'].sort(key=digest)
    result['ports'].sort(key=digest)
    return result


def selected_port(service):
    candidates = []
    for port in service['ports']:
        if port.get('protocol', 'TCP') != 'TCP': continue
        app = port.get('appProtocol'); port_name = port.get('name', '')
        # appProtocol is authoritative; never guess a scheme from a port number.
        scheme = app if app in ('http', 'https') else None
        if app is None:
            scheme = 'https' if port_name == 'https' or port_name.startswith('https-') else \
                     'http' if port_name == 'http' or port_name.startswith('http-') else None
        if scheme and (service['port_hint'] is None or port_name == service['port_hint']): candidates.append((port, scheme))
    require(len(candidates) == 1, 'SERVICE_HTTP_PORT_MISSING_OR_AMBIGUOUS')
    port, scheme = candidates[0]
    require(type(port.get('port')) is int and 1 <= port['port'] <= 65535, 'INVALID_SERVICE_PORT')
    if 'name' in port: name(port['name'])
    target = port.get('targetPort', port['port'])
    require(type(target) is int and 1 <= target <= 65535 or isinstance(target, str) and bool(name(target)), 'INVALID_SERVICE_PORT')
    return port, scheme


def endpoint(service):
    require(service['service_type'] in ('ClusterIP','NodePort','LoadBalancer') and service['cluster_ip'] not in (None,'None'),
            'SERVICE_CLUSTER_IP_REQUIRED')
    require(bool(service['selector']) and not service['publish_not_ready'], 'SERVICE_READINESS_CONTRACT_UNSUPPORTED')
    try: ip = ipaddress.ip_address(service['cluster_ip'])
    except ValueError: raise ReadFailure('INVALID_SERVICE_CLUSTER_IP') from None
    require(not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast), 'INVALID_SERVICE_CLUSTER_IP')
    port, scheme = selected_port(service)
    ready, slice_ids = set(), []
    for row in service['slices']:
        require(row['address_type'] in ('IPv4','IPv6'), 'ENDPOINTSLICE_ADDRESS_TYPE_UNSUPPORTED')
        selected = [p for p in row['ports'] if (p.get('name') or '') == (port.get('name') or '') and p.get('protocol','TCP') == 'TCP']
        require(len(selected) == 1 and type(selected[0].get('port')) is int and 1 <= selected[0]['port'] <= 65535,
                'ENDPOINTSLICE_PORT_MISMATCH')
        target_port = port.get('targetPort', port['port'])
        if type(target_port) is int: require(selected[0]['port'] == target_port, 'ENDPOINTSLICE_PORT_MISMATCH')
        slice_ids.append(row['uid'])
        for e in row['endpoints']:
            conditions = e['conditions']
            require(all(v is None or type(v) is bool for v in conditions.values()), 'INVALID_ENDPOINTSLICE_CONDITION')
            addresses = []
            for address in e['addresses']:
                try: parsed = ipaddress.ip_address(address)
                except ValueError: raise ReadFailure('INVALID_ENDPOINTSLICE_ADDRESS') from None
                require('IPv'+str(parsed.version) == row['address_type'] and not
                        (parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified or parsed.is_multicast), 'INVALID_ENDPOINTSLICE_ADDRESS')
                addresses.append(parsed)
            if conditions.get('ready') is not True or conditions.get('terminating') is True or conditions.get('serving') is False: continue
            target = e['target']
            require(target.get('kind') == 'Pod' and target.get('namespace') == service['namespace'], 'ENDPOINTSLICE_POD_IDENTITY_REQUIRED')
            name(target.get('name')); pod_uid = uid(target.get('uid'))
            if any(a.version == ip.version for a in addresses): ready.add(pod_uid)
    require(ready, 'SERVICE_READY_ENDPOINT_MISSING')
    host = '['+str(ip)+']' if ip.version == 6 else str(ip)
    return scheme+'://'+host+':'+str(port['port']), {
        'source':'service_cluster_ip_and_owned_ready_endpointslices','service_port':port['port'],
        'endpoint_slice_uids':sorted(slice_ids),'ready_pod_count':len(ready)}


def inventory(client, namespaces, installation):
    binding = client.binding()
    cluster_uid = uid(client.namespace('kube-system'))
    services = [project_service(row) for row in client.services(namespaces)]
    if namespaces: require(all(s['namespace'] in namespaces for s in services), 'KUBERNETES_SERVICE_SCOPE_MISMATCH')
    candidates = sorted({s['installation'] for s in services})
    require(services, 'KUBERNETES_INSTALLATION_NOT_FOUND')
    if installation is None:
        require(len(candidates) == 1, 'KUBERNETES_INSTALLATION_AMBIGUOUS')
        installation = candidates[0]
    services = [s for s in services if s['installation'] == installation]
    require(services, 'KUBERNETES_INSTALLATION_NOT_FOUND')
    ids = [s['uid'] for s in services]
    require(len(set(ids)) == len(ids), 'KUBERNETES_DUPLICATE_OBJECT')
    ns_uids = {ns:uid(client.namespace(ns)) for ns in sorted({s['namespace'] for s in services})}
    for service in services:
        raw = client.slices(service['namespace'], service['name'])
        require(len(raw) <= 128, 'ENDPOINTSLICE_SIZE_LIMIT')
        service['slices'] = sorted([slice_projection(row, service) for row in raw], key=lambda s:s['uid'])
        require(len({s['uid'] for s in service['slices']}) == len(service['slices']), 'KUBERNETES_DUPLICATE_OBJECT')
        service['ports'].sort(key=digest)
    return {'binding':binding,'cluster_namespace_uid':cluster_uid,'namespace_uids':ns_uids,
            'installation':installation,'services':sorted(services,key=lambda s:s['uid'])}


def discover_kubernetes(cfg=None, client=None, namespaces=None, installation=None, transport_factory=Transport):
    from ops_platform.ops.discover import health, configured_url
    cfg = cfg or {'platforms':{r:{'url':None} for r in PLATFORMS},'timeout':5}
    namespaces = sorted(set(name(ns) for ns in (namespaces or [])))
    require(len(namespaces) <= 16, 'KUBERNETES_NAMESPACE_LIMIT')
    if installation is not None: label(installation)
    require((cfg.get('resource_profile') or {}).get('environment','kubernetes') == 'kubernetes', 'CONFIG_ENVIRONMENT_MISMATCH')
    client = client or Kubectl()
    report = {'schema':1,'stage':'O04-environment-discovery','provider':'kubernetes',
        'scope':'kubernetes_service_metric_proposal_only','result':'attention_required',
        'checked_at':datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),
        'services':{},'metrics':None,'connection_overlay':None,
        'namespace_scope':namespaces or 'installation_labels_across_namespaces',
        'service_label_selector':PART_OF+'=agent-project','installation_filter':installation,
        'watch_enabled':False,'kubernetes_activation_supported':False,
        'activation_requested':False,'automatic_activation_supported':False,'files_changed':[],
        'service_control_requested':False,'inference_requested':False,'application_credentials_sent':False,
        'control_plane_authentication':'existing_kubectl_context','control_plane_methods':['GET'],
        'configured_profile_preserved':'resource_profile' in cfg,'deployment_metadata_stable':False}
    try:
        before = inventory(client, namespaces, installation)
        report['kubernetes'] = {k:deepcopy(before[k]) for k in ('binding','cluster_namespace_uid','namespace_uids','installation')}
        proposed = {}
        for role in ROLES:
            rows = [s for s in before['services'] if s['role'] == role]
            item = {'status':'unavailable','candidate_count':len(rows)}; report['services'][role] = item
            try:
                require(len(rows) == 1, 'SERVICE_MISSING' if not rows else 'SERVICE_AMBIGUOUS')
                service = rows[0]
                url, evidence = endpoint(service)
                identity = {'provider':'kubernetes','cluster_namespace_uid':before['cluster_namespace_uid'],
                    'namespace':service['namespace'],'namespace_uid':before['namespace_uids'][service['namespace']],
                    'service':service['name'],'service_uid':service['uid'],'installation':before['installation'],'role':role}
                item.update(endpoint=url,identity=identity,evidence=evidence)
                health(role, transport_factory(url, timeout=cfg['timeout']))
                old = configured_url(cfg, role)
                item.update(status='verified',configured_endpoint=old,reconciliation_required=old is not None and old.rstrip('/') != url)
                proposed[role] = url
            except ReadFailure as exc: item['error'] = exc.code
        if 'prometheus' in proposed:
            try:
                report['metrics'] = discover_metrics(transport_factory(proposed['prometheus'], timeout=cfg['timeout']),
                                                      cfg.get('resource_profile'), environment='kubernetes')
                report['metrics']['api_cluster_metric_label_binding'] = 'not_verified'
            except ReadFailure as exc: report['metrics'] = {'status':'unavailable','error':exc.code,'generated_profile':None}
        after = inventory(client, namespaces, installation)
        stable = before == after; report['deployment_metadata_stable'] = stable
        report['deployment_fingerprint'] = digest(before)
        if not stable: report['error'] = 'KUBERNETES_BINDING_CHANGED_DURING_DISCOVERY'
        if stable and len(proposed) == len(ROLES) and (report['metrics'] or {}).get('status') == 'verified':
            overlay = {'platforms':{r:{'url':proposed[r]} for r in PLATFORMS},
                       **{r+'_url':proposed[r] for r in ROLES[4:]}}
            if 'resource_profile' not in cfg: overlay['resource_profile'] = report['metrics']['generated_profile']
            report.update(result='discovered',connection_overlay=overlay)
    except (ReadFailure,OSError,ValueError,KeyError,TypeError,AttributeError) as exc:
        report['error'] = exc.code if isinstance(exc,ReadFailure) else 'INVALID_KUBERNETES_RESPONSE'
    report['kubernetes_api_requests'] = client.requests
    return report
