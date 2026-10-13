#!/usr/bin/env python3
"""Read-only checks for infra n8n, including storage and settings boundaries."""
import argparse
import json
import socket
import subprocess
from pathlib import Path
from common import ROOT, config, run
from configure import check_files, validate

IMAGE = 'n8nio/n8n:2.40.5@sha256:9f693fd5565539efd5e75ad168526c8041a6af516d9e50bc4d9cb1c9c5031523'


def verify(c, root=ROOT):
    s = c['services']
    if set(s) != {'n8n','flows-postgres'}: raise ValueError('Unexpected infra n8n services')
    if c['name'] != 'infrastructure': raise ValueError('n8n must be owned by infrastructure')
    if not c['networks']['flows-private'].get('internal'): raise ValueError('n8n DB network must be internal')
    if set(s['flows-postgres']['networks']) != {'flows-private'}: raise ValueError('n8n DB must stay private')
    if set(s['n8n']['networks']) != {'flows-private','flows-api'}: raise ValueError('Unexpected n8n networks')
    for name, v in s.items():
        if name != 'n8n' and v.get('ports'): raise ValueError('Only n8n may publish a port')
        if v.get('privileged') or v.get('devices') or v.get('gpus'): raise ValueError('Host/GPU access is not used')
        for m in v.get('volumes', []):
            if 'docker.sock' in m.get('target', '') or 'docker.sock' in m.get('source', ''): raise ValueError('Docker socket is not used')
            if m.get('type') == 'bind' and (not m.get('read_only') or not Path(m['source']).is_relative_to(root)):
                raise ValueError('Only readonly infra/n8n scripts can be mounted')
        if any(x['source'] not in ('flows-db-admin','flows-db-app','flows-encryption') for x in v.get('secrets', [])):
            raise ValueError('Foreign platform secret detected')
    if {x['source'] for x in s['n8n']['secrets']} != {'flows-db-app','flows-encryption'}:
        raise ValueError('n8n must use only its own DB and encryption key')
    env = s['n8n']['environment']
    if (env['DB_POSTGRESDB_HOST'],env['DB_POSTGRESDB_DATABASE'],env['DB_POSTGRESDB_USER'],env['DB_POSTGRESDB_SCHEMA']) != ('flows-postgres','flows','flows_service','n8n'):
        raise ValueError('n8n must use its dedicated DB/account/schema')
    if s['n8n']['image'] != IMAGE: raise ValueError('Unexpected n8n image')
    for key in ('flows-db','flows-data'):
        if not c['volumes'][key].get('external'): raise ValueError('n8n volumes must be external and preserved')
    values = check_files(root); validate(values)
    if [c['volumes'][k]['name'] for k in ('flows-db','flows-data')] != [values['N8N_DB_VOLUME'],values['N8N_DATA_VOLUME']]:
        raise ValueError('Compose overrides changed the configured volumes')
    if c['networks']['flows-api']['name'] != values['N8N_AGENT_NETWORK']: raise ValueError('Network override mismatch')
    for spec in c['secrets'].values():
        p = Path(spec['file'])
        if (p.parent != root/'.secrets' or not p.is_file() or p.is_symlink()
                or p.parent.stat().st_mode & 0o077 or p.stat().st_mode & 0o444 != 0o444):
            raise ValueError('Missing/unsafe n8n secret')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--skip-port-check', action='store_true'); a = p.parse_args()
    c = config(); verify(c)
    run(['docker','network','inspect',c['networks']['flows-api']['name']])
    for v in c['volumes'].values(): run(['docker','volume','inspect',v['name']])
    if not a.skip_port_check:
        for m in c['services']['n8n']['ports']:
            with socket.socket() as sock: sock.bind((m.get('host_ip','0.0.0.0'),int(m['published'])))
    print('PASS: infra-owned n8n; dedicated DB/secrets; existing external volumes')

if __name__ == '__main__':
    try: main()
    except (ValueError,OSError,KeyError,subprocess.SubprocessError) as e: raise SystemExit('FAIL: '+str(e))
