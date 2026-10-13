#!/usr/bin/env python3
"""Start shared infrastructure, or an explicitly selected n8n/observability group."""
import argparse
import fcntl
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group()
    group.add_argument('--n8n-only', action='store_true', help='Migrate/start only n8n and its DB; keep other running infra services untouched')
    group.add_argument('--observability-only', action='store_true', help='Prepare/start only O01 host/container resource monitoring')
    group.add_argument('--gpu-only', action='store_true', help='Start GPU telemetry and attach it to the existing Prometheus')
    group.add_argument('--langfuse-only', action='store_true', help='Prepare/start only the isolated Langfuse infrastructure group')
    a = p.parse_args()
    if a.langfuse_only:
        return subprocess.call([sys.executable, '-B', str(ROOT/'langfuse/ops/manage.py'), 'up'])
    if a.gpu_only:
        return subprocess.call([sys.executable, '-B', str(ROOT/'observability/ops/gpu.py'), 'up'])
    if a.observability_only:
        # Do not import, migrate or diagnose n8n when selecting the observation group.
        return subprocess.call([sys.executable, '-B', str(ROOT/'observability/ops/manage.py'), 'up'])
    sys.path.insert(0,str(ROOT/'n8n/ops'))
    from lifecycle import start
    lock = ROOT/'n8n/.ops.lock'
    fd = os.open(lock, os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd, 'w') as f:
        try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('Another infra n8n operation is running')
        start(n8n_only=a.n8n_only)
    return 0

if __name__ == '__main__':
    try: sys.exit(main())
    except (ValueError,OSError,KeyError,subprocess.SubprocessError) as e:
        if any(option in sys.argv for option in ('--observability-only', '--gpu-only', '--langfuse-only')):
            print('FAIL: selected infra launcher failed: '+str(e), file=sys.stderr)
            sys.exit(1)
        sys.path.insert(0,str(ROOT/'n8n/ops'))
        from common import redact
        from diagnose import report
        print('FAIL: '+redact(e), file=sys.stderr)
        try: report()
        except Exception: print('Diagnostics unavailable; run python3 src/infra/n8n/ops/diagnose.py', file=sys.stderr)
        sys.exit(1)
