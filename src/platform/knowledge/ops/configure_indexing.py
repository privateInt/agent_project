#!/usr/bin/env python3
"""Configure the existing TLS OpenSearch cluster; never rebuild infrastructure."""
import argparse
import base64
import json
from pathlib import Path
import ssl
import subprocess
from configure import create_private, dotenv


def settings(config):
    node = config.get('services', {}).get('opensearch-node1', {})
    password = node.get('environment', {}).get('OPENSEARCH_INITIAL_ADMIN_PASSWORD')
    if not isinstance(password, str) or not password or any(c in password for c in '\r\n\x00'):
        raise ValueError('OpenSearch credentials could not be resolved')
    mounts = [v for v in node.get('volumes', []) if isinstance(v, dict) and v.get('target') == '/usr/share/opensearch/config/certs/rootCA.pem' and v.get('type') == 'bind']
    if len(mounts) != 1:
        raise ValueError('OpenSearch public root CA bind mount is required')
    ca = Path(mounts[0]['source'])
    if not ca.is_file() or ca.stat().st_size > 65536:
        raise ValueError('Public CA must be a file no larger than 64 KiB')
    raw = ca.read_bytes()
    if b'PRIVATE KEY' in raw:
        raise ValueError('Only the public CA certificate is accepted')
    ssl.create_default_context(cadata=raw.decode('ascii'))
    network = config.get('networks', {}).get('opensearch-net', {}).get('name')
    if not network or any(c in network for c in '\r\n\x00'):
        raise ValueError('OpenSearch network could not be resolved')
    return {
        'KNOWLEDGE_OPENSEARCH_ENDPOINT': 'https://opensearch-node1:9200',
        'KNOWLEDGE_OPENSEARCH_USERNAME': 'admin',
        'KNOWLEDGE_OPENSEARCH_PASSWORD': password,
        'KNOWLEDGE_OPENSEARCH_CA_BASE64': base64.b64encode(raw).decode('ascii'),
        'KNOWLEDGE_OPENSEARCH_NETWORK': network,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--opensearch', type=Path, default=Path('../src/infra/opensearch'))
    args = parser.parse_args()
    target = Path(__file__).resolve().parents[1] / '.env.indexing'
    if target.exists():
        raise ValueError('.env.indexing already exists; no settings were overwritten')
    rendered = subprocess.run(['docker', 'compose', 'config', '--format', 'json'], cwd=args.opensearch.resolve(), capture_output=True, text=True, timeout=60)
    if rendered.returncode:
        raise ValueError('Cannot resolve OpenSearch Compose configuration')
    values = settings(json.loads(rendered.stdout))
    create_private(target, dotenv(values))
    try:
        checked = subprocess.run(['docker', 'compose', '--env-file', '.env', '--env-file', '.env.indexing', '-f', 'docker-compose.yml', '-f', 'indexing.compose.yml', 'config', '--format', 'json'], cwd=target.parent, capture_output=True, text=True, timeout=60)
        if checked.returncode:
            raise ValueError('Indexing Compose configuration is invalid')
        config = json.loads(checked.stdout)
        actual = config['services']['knowledge-service']['environment']
        for suffix in ('ENDPOINT', 'USERNAME', 'PASSWORD', 'CA_BASE64'):
            if actual.get('OPENSEARCH_' + suffix) != values['KNOWLEDGE_OPENSEARCH_' + suffix]:
                raise ValueError('Compose changed an OpenSearch connection setting')
        if config['networks']['knowledge-search']['name'] != values['KNOWLEDGE_OPENSEARCH_NETWORK']:
            raise ValueError('Compose changed the OpenSearch network')
    except Exception:
        target.unlink()
        raise ValueError('Configuration validation failed; newly created settings were removed') from None
    print('Created private indexing connection settings (0600). No services or infrastructure were changed.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError, ssl.SSLError) as exc:
        raise SystemExit('FAIL: ' + str(exc))
