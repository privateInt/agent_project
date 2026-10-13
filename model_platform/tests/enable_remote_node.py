"""Pin an existing disabled node after a successful read-only probe.
Does not connect to hosts, start work, or change a previously pinned daemon ID.
Recreate Training, Dispatcher and Reconciler after changing the mounted file.
"""
import argparse
import json
from pathlib import Path


def enable(catalog, probe):
    if (catalog.get('version') != 1 or probe.get('version') != 1
            or probe.get('action') != 'probe' or probe.get('status') != 'ready'
            or probe.get('architecture') != 'amd64' or not probe.get('daemon_id')):
        raise ValueError('a successful amd64 node probe is required')
    matches = [n for n in catalog['nodes'] if n['name'] == probe.get('node_name')]
    if len(matches) != 1:
        raise ValueError('probe must identify exactly one existing node')
    node = matches[0]
    if node['architecture'] != probe['architecture']:
        raise ValueError('node architecture changed')
    if node.get('daemon_id') and node['daemon_id'] != probe['daemon_id']:
        raise ValueError('refusing to rebind an existing node to another Docker daemon')
    if any(n is not node and n.get('daemon_id') == probe['daemon_id'] for n in catalog['nodes']):
        raise ValueError('Docker daemon already registered under a different node name')
    node.update(daemon_id=probe['daemon_id'], enabled=True)
    return catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', required=True, type=Path)
    parser.add_argument('--config', type=Path, default=Path('remote/execution-nodes.json'))
    args = parser.parse_args()
    catalog = json.loads(args.config.read_text())
    probe = json.loads(args.probe.read_text())
    updated = enable(catalog, probe)
    temp = args.config.with_suffix('.tmp')
    temp.write_text(json.dumps(updated, indent=2) + '\n')
    temp.replace(args.config)
    print('Pinned and enabled node ' + probe['node_name'] + '; recreate central services before submitting work.')


if __name__ == '__main__':
    main()
