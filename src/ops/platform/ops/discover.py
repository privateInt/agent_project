#!/usr/bin/env python3
"""Discover and validate a connection proposal; never activate or restart it."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from src.infra.observability.ops.backup_preflight import Unavailable
from src.ops.platform.contracts import ReadFailure, require
from src.ops.platform.ops import manage
from src.ops.platform.ops.discovery_compose import DockerDiscovery, ROLES, belongs, endpoints, scoped
from src.ops.platform.ops.discovery_metrics import discover_metrics
from src.ops.platform.transport import Transport


def health(role, transport):
    path = {'agent': '/healthz', 'prometheus': '/api/v1/status/buildinfo',
            'alertmanager': '/api/v2/status',
            'langfuse': '/api/public/health?failIfDatabaseUnavailable=true'}.get(role, '/health')
    _, body = transport.get(path)
    if role == 'prometheus':
        ok = body.get('status') == 'success' and isinstance(body.get('data'), dict) and bool(body['data'].get('version'))
    elif role == 'alertmanager':
        ok = isinstance(body.get('versionInfo'), dict) and bool(body['versionInfo'].get('version'))
    elif role == 'langfuse':
        ok = body.get('status') == 'OK'
    else:
        ok = body.get('status') == 'ok'
        if role == 'model': ok = ok and body.get('service') == 'model-gateway'
        if role == 'agent': ok = ok and body.get('platform') == 'agent'
    require(ok, 'SERVICE_HEALTH_CONTRACT_MISMATCH')


def configured_url(cfg, role):
    return cfg['platforms'][role]['url'] if role in cfg['platforms'] else cfg.get(role + '_url')


def discover(cfg, root, docker, transport_factory=Transport):
    require((cfg.get('resource_profile') or {}).get('environment', 'docker-compose') == 'docker-compose',
            'KUBERNETES_DISCOVERY_NOT_IMPLEMENTED')
    docker.host()
    before = scoped(docker.inventory(), root)
    report = {'schema': 1, 'stage': 'O04-environment-discovery', 'result': 'attention_required',
              'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
              'provider': 'docker-compose', 'scope': 'existing_prepared_installation_proposal_only',
              'services': {}, 'metrics': None, 'connection_overlay': None,
              'activation_requested': False, 'files_changed': [], 'service_control_requested': False,
              'inference_requested': False, 'credentials_sent': False,
              'configured_profile_preserved': 'resource_profile' in cfg}
    proposed = {}
    for role in ROLES:
        rows = [row for row in before if belongs(row, root, role)]
        item = {'status': 'unavailable', 'candidate_count': len(rows)}
        report['services'][role] = item
        try:
            require(len(rows) == 1, 'SERVICE_MISSING' if not rows else 'SERVICE_AMBIGUOUS')
            row = rows[0]
            require(row['state'] == 'running', 'SERVICE_NOT_RUNNING')
            url, source = endpoints(row, role)
            health(role, transport_factory(url, timeout=cfg['timeout']))
            old = configured_url(cfg, role)
            item.update(status='verified', endpoint=url, configured_endpoint=old,
                        reconciliation_required=old is not None and old.rstrip('/') != url,
                        identity={'provider': 'docker-compose', 'project': row['project'], 'service': row['service']},
                        evidence={'source': source, 'container_id': row['id']})
            proposed[role] = url
        except ReadFailure as exc:
            item['error'] = exc.code
            if exc.http_status is not None: item['http_status'] = exc.http_status
    if 'prometheus' in proposed:
        try:
            report['metrics'] = discover_metrics(transport_factory(proposed['prometheus'], timeout=cfg['timeout']),
                                                  cfg.get('resource_profile'))
        except ReadFailure as exc:
            report['metrics'] = {'status': 'unavailable', 'error': exc.code, 'generated_profile': None}
            if exc.http_status is not None: report['metrics']['http_status'] = exc.http_status
    stable = before == scoped(docker.inventory(), root)
    report['deployment_metadata_stable'] = stable
    ready = stable and len(proposed) == len(ROLES) and (report['metrics'] or {}).get('status') == 'verified'
    if ready:
        # A patch-shaped proposal, NOT a complete config. Credentials/public URL/cache
        # remain in existing config. Existing explicit profile always wins.
        overlay = {'platforms': {role: {'url': proposed[role]} for role in cfg['platforms']},
                   **{role + '_url': proposed[role] for role in ('prometheus', 'alertmanager', 'langfuse')}}
        if 'resource_profile' not in cfg:
            overlay['resource_profile'] = report['metrics']['generated_profile']
        report.update(result='discovered', connection_overlay=overlay)
    if not stable: report['error'] = 'DEPLOYMENT_CHANGED_DURING_DISCOVERY'
    report['automatic_activation_supported'] = False
    return report


def config_stamp(directory):
    names = ('owner.json', 'host-config.json', 'config.json', 'compose.env')
    return {name: hashlib.sha256(manage.private(directory / name).read_bytes()).hexdigest() for name in names}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', default=str(manage.STATE))
    parser.add_argument('--provider', choices=('auto', 'docker-compose', 'kubernetes'), default='auto')
    parser.add_argument('--context', help='Kubernetes context; default: current context')
    parser.add_argument('--namespace', action='append', default=[], help='Optional namespace restriction; repeatable')
    parser.add_argument('--installation', help='Select only when multiple installation labels exist')
    args = parser.parse_args()
    try:
        directory = manage.safe(args.state_dir)
        if args.provider == 'kubernetes':
            from src.ops.platform.ops.discovery_kubernetes import discover_kubernetes
            from src.ops.platform.ops.kubernetes_client import Kubectl
            present = directory.exists()
            start = config_stamp(directory) if present else None
            result = discover_kubernetes(client=Kubectl(args.context), namespaces=args.namespace, installation=args.installation)
            stable = directory.exists() == present and start == (config_stamp(directory) if present else None)
            result['prepared_configuration_stable'] = stable if present else None
        else:
            require(not (args.context or args.namespace or args.installation), 'KUBERNETES_OPTIONS_ONLY')
            start = config_stamp(directory)
            cfg = manage.existing(directory)
            result = discover(cfg, ROOT, DockerDiscovery())
            stable = start == config_stamp(directory)
            result['prepared_configuration_stable'] = stable
        if not stable:
            result.update(result='attention_required', connection_overlay=None, error='CONFIG_CHANGED_DURING_DISCOVERY')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['result'] == 'discovered' else 2
    except (ReadFailure, Unavailable, OSError, ValueError, TypeError, KeyError) as exc:
        code = exc.code if isinstance(exc, ReadFailure) else str(exc) if isinstance(exc, Unavailable) else 'DISCOVERY_INPUT_UNAVAILABLE'
        print(json.dumps({'schema': 1, 'stage': 'O04-environment-discovery', 'result': 'error', 'error': code,
                          'activation_requested': False, 'service_control_requested': False, 'inference_requested': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
