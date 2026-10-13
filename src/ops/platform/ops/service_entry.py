#!/usr/bin/env python3
"""Internal systemd entry; never run this instead of the lifecycle command."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from src.ops.platform.contracts import require
from src.ops.platform.ops import service_lifecycle as service, lifecycle_state as local
from src.ops.platform.ops import worker_state as ws, refresh_worker as worker


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = service.load(args.state_dir)
        require(manifest['phase'] == 'installed', 'SERVICE_NOT_INSTALLED')
        require(service.unit_bytes(Path(manifest['unit_path'])) == service.render(manifest), 'SERVICE_UNIT_CHANGED')
        invocation = os.environ.get('INVOCATION_ID', '')
        require(re.fullmatch('[0-9a-f]{32}', invocation) is not None, 'SERVICE_MANAGER_INVOCATION_REQUIRED')
        require(ws.Store(args.state_dir).manifest()['binding'] == manifest['binding'], 'SERVICE_BINDING_CHANGED')
        boot = None
        if manifest['persistence'] == 'boot':
            from src.ops.platform.ops.boot_lifecycle import Context
            boot = Context(args.state_dir, manifest)
        result = worker.run(args.state_dir, interval=manifest['interval_seconds'],
                            deadline_seconds=manifest['deadline_seconds'], service_invocation=invocation,
                            boot_context=boot)
        print(json.dumps(result)); return result['exit_code']
    except local.ERRORS as exc:
        # Gate failures use exit 2, excluded from automatic systemd restarts.
        print(json.dumps({'stage': service.STAGE, 'result': 'attention_required', 'code': local.error_code(exc)}))
        return 2


if __name__ == '__main__': raise SystemExit(main())
