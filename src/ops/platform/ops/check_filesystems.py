#!/usr/bin/env python3
"""GET-only acceptance of /mnt/nvme1 resource coverage and the existing alert endpoint."""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from src.ops.platform.contracts import ReadFailure
from src.ops.platform.settings import load
from src.ops.platform.transport import Transport


def summarize(resources, alerts):
    source = resources.get('metrics', {}).get('filesystems', {})
    data = source.get('data') or {}
    rows = data.get('items', [])
    now = datetime.now(timezone.utc).timestamp()
    def valid(row):
        try:
            value = row.get('value')
            age = now - datetime.fromisoformat(row['evaluated_at'].replace('Z', '+00:00')).timestamp()
            return (row.get('value_status') == 'observed' and type(value) in (int, float)
                    and math.isfinite(value) and 0 <= value <= 100 and -1 <= age < 90)
        except (ValueError, KeyError, TypeError):
            return False
    mounts = {r.get('labels', {}).get('mountpoint') for r in rows}
    rules = (alerts.get('sources', {}).get('prometheus', {}).get('data') or {}).get('items', [])
    fs = [r for r in rules if r.get('id') == 'OpsFilesystemHigh']
    good = (source.get('status') == 'ok' and data.get('coverage') == 'query_result'
            and {'/', '/mnt/nvme1'} <= mounts and all(valid(r) for r in rows)
            and alerts.get('status') == 'ok' and len(fs) == 1
            and fs[0].get('health') == 'ok' and fs[0].get('hold_seconds') == 900)
    return {'schema': 1, 'stage': 'O04-filesystem-ops-readback',
            'result': 'passed' if good else 'attention_required',
            'checked_at': resources.get('checked_at'), 'filesystem_source_status': source.get('status'),
            'filesystem_error': source.get('error'), 'filesystems': [
                {'labels': r.get('labels'), 'usage_percent': r.get('value'), 'value_status': r.get('value_status')}
                for r in rows], 'filesystem_rule': fs[0] if len(fs) == 1 else None,
            'alert_source_status': {k: {'status': v.get('status'), 'error': v.get('error')}
                                    for k, v in alerts.get('sources', {}).items()},
            'scope': 'coverage_and_readback_not_disk_cleanup_or_alert_resolution',
            'external_delivery': 'not_configured', 'business_api_requests': 0, 'inference_requested': False,
            'browser_acceptance': 'deferred_by_user'}


def check(cfg):
    url = cfg['public_url'] if cfg['tls_cert'] else 'http://127.0.0.1:' + str(cfg['port'])
    api = Transport(url, cfg['access_file'], timeout=30)
    _, resources = api.get('/api/resources')
    _, alerts = api.get('/api/alerts')
    return summarize(resources, alerts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path,
                        default=Path.home() / '.local/state/agent-project-ops/server/host-config.json')
    args = parser.parse_args()
    result = check(load(args.config))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['result'] == 'passed' else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ReadFailure, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'result': 'error', 'code': exc.code if isinstance(exc, ReadFailure)
                          else 'LOCAL_CONFIG_OR_RESPONSE_ERROR'}))
        raise SystemExit(1)
