"""Explicit operational commands; status never starts model/GPU Jobs."""
import argparse
import json
import os
from pathlib import Path
import sys
import uuid
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from common import Context, DATABASES, OpsError, ROOT, docker, private_json, public_container, require


def pressure(stats, minimum_gib=20):
    available = stats['available_bytes']
    percent = available / max(stats['total_bytes'], 1) * 100
    inode_percent = stats['available_inodes'] / max(stats['inodes'], 1) * 100 if stats['inodes'] else 100
    if available < minimum_gib * 1024 ** 3 or percent < 5 or inode_percent < 5:
        return 'critical'
    return 'warning' if percent < 10 or inode_percent < 10 else 'ok'


def api_get(base, path):
    from urllib.parse import urlsplit
    parsed = urlsplit(base)
    require(parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost')
            and not parsed.username and not parsed.password and parsed.path in ('', '/')
            and not parsed.query and not parsed.fragment, 'Use the local Gateway HTTP endpoint')
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    with build_opener(ProxyHandler({}), NoRedirect()).open(Request(base.rstrip('/') + path, method='GET'), timeout=15) as response:
        require(response.status == 200 and response.geturl() == base.rstrip('/') + path, 'Unexpected API response')
        return json.load(response)


def audit(ctx, api):
    result = {'scope': 'read-only-operational-audit', 'services': {}, 'catalog': {}, 'volumes': {},
              'notes': ['Filesystem capacity is shared; do not add the free space of different volumes',
                        'Thresholds are operational recommendations, not a guarantee that MinIO accepts writes'], 'passed': True}
    for name, item in ctx.services.items():
        row = public_container(item)
        row['healthy'] = row['state'] == 'running' and row['health'] not in ('starting', 'unhealthy')
        result['services'][name] = row
        result['passed'] &= row['healthy']
    api_get(api, '/health')
    for path, field in (('models', 'items'), ('model-versions', 'items'), ('training-runs', 'items'),
                        ('deployments', 'items'), ('operations', 'items'), ('datasets', 'datasets')):
        page = api_get(api, '/api/' + path + '?limit=1')
        require(isinstance(page.get(field), list), 'Invalid catalog envelope: ' + path)
        result['catalog'][path] = {'responding': True, 'sample_count': len(page[field])}
    for kind, volume in ctx.volumes.items():
        stats = json.loads(ctx.helper(volume, 'scan', timeout=600))
        stats['pressure'] = pressure(stats)
        result['volumes'][kind] = stats
        result['passed'] &= stats['pressure'] != 'critical'
    # The backing devices of MinIO bind mounts are not the S3 logical bucket sizes.
    result['minio_paths'] = []
    for name in ('ops-minio1', 'ops-minio2', 'ops-minio3', 'ops-minio4'):
        item = ctx.by_name.get(name)
        require(item is not None, 'Missing MinIO node: ' + name)
        for m in item.get('Mounts', []):
            if m['Destination'] == '/data':
                row = {'container': name, 'path': m['Source'], 'type': m['Type']}
                try:
                    st = os.statvfs(m['Source'])
                    row.update(total_bytes=st.f_blocks * st.f_frsize, available_bytes=st.f_bavail * st.f_frsize,
                               inodes=st.f_files, available_inodes=st.f_favail)
                    row['pressure'] = pressure(row)
                    result['passed'] &= row['pressure'] != 'critical'
                except OSError:
                    row['pressure'] = 'unavailable'
                    result['passed'] = False
                result['minio_paths'].append(row)
    result['gc_mode'] = ctx.config['services']['gc']['environment']['GC_MODE']
    return result


def clean_cache(ctx, dep_id, apply):
    require(str(uuid.UUID(dep_id)) == dep_id, 'Canonical deployment ID required')
    ctx.cold()
    # Cold services, no producers, stopped desired state and stopped runtime are all required.
    state = ctx.sql('model_deployment', "SELECT desired_state || '/' || runtime_state FROM deployments WHERE deployment_id='" + dep_id + "'::uuid")
    require(state == 'stopped/stopped', 'The selected deployment must be durably stopped')
    result = {'deployment_id': dep_id, 'volume': ctx.volumes['serving-cache'], 'apply': apply,
              'scope': 'only this stopped deployment cache; models, logs and training outputs retained'}
    if apply:
        ctx.cold()
        result['result'] = json.loads(ctx.helper(ctx.volumes['serving-cache'], 'delete-cache', args=(dep_id,)))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('audit', help='GET APIs, inspect containers and read storage usage')
    p.add_argument('--api', default='http://127.0.0.1:13000')
    p.add_argument('--report', required=True)
    p = sub.add_parser('backup', help='Requires idle Jobs, stopped platform services and stopped MLflow')
    p.add_argument('--destination', required=True)
    p.add_argument('--s3-endpoint', required=True, help='Host-reachable S3 API endpoint, no credentials in URL')
    p = sub.add_parser('verify-backup')
    p.add_argument('--backup', required=True)
    p = sub.add_parser('restore-drill', help='New temporary PostgreSQL/MinIO/volumes only')
    p.add_argument('--backup', required=True)
    p.add_argument('--report', required=True)
    p = sub.add_parser('clean-cache', help='Offline targeted cache cleanup; default is plan only')
    p.add_argument('--deployment-id', required=True)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--report', required=True)
    args = parser.parse_args()
    # Avoid running a mutation successfully and then losing its receipt to an old report path.
    if hasattr(args, 'report'):
        require(not Path(args.report).exists(), 'Use a new report filename')
    if args.command == 'verify-backup':
        import backup
        data = backup.verify(args.backup)
        result = {'passed': True, 'files': len(data['files']), 'bytes': sum(f['size'] for f in data['files']),
                  'scope': 'local file integrity; actual restore requires restore-drill'}
    elif args.command == 'backup':
        import backup
        data = backup.create(Context(), args.destination, args.s3_endpoint)
        result = {'passed': True, 'backup': str(Path(args.destination).absolute()), 'files': len(data['files'])}
    elif args.command == 'restore-drill':
        import restore_drill
        result = restore_drill.execute(args.backup)
    elif args.command == 'clean-cache':
        result = clean_cache(Context(), args.deployment_id, args.apply)
    else:
        result = audit(Context(), args.api)
    if hasattr(args, 'report'):
        private_json(args.report, result)
        print('[report] ' + str(Path(args.report).absolute()))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get('passed', True) else 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        # SDK exceptions can include object paths, endpoint or credentials. Keep them local.
        message = str(exc) if isinstance(exc, OpsError) else type(exc).__name__
        print('FAIL: ' + message, file=sys.stderr)
        sys.exit(1)
