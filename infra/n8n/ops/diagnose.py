#!/usr/bin/env python3
"""Print bounded n8n state, health output and startup logs with known secrets removed."""
import json
import subprocess
from common import containers, redact, run


def report():
    found = []
    for project in ('agent-flows', 'infrastructure'):
        for c in containers(['label=com.docker.compose.project='+project]):
            labels = c.get('Config', {}).get('Labels', {})
            if labels.get('com.docker.compose.service') not in ('n8n', 'flows-postgres'): continue
            state = c.get('State', {})
            item = {'container': c['Name'].lstrip('/'), 'project': project,
                    'state': state.get('Status'), 'exit_code': state.get('ExitCode'),
                    'restart_count': c.get('RestartCount'),
                    'health': state.get('Health', {}).get('Status'),
                    'health_log': [{k: v for k, v in x.items() if k in ('End','ExitCode','Output')}
                                   for x in state.get('Health', {}).get('Log', [])[-3:]],
                    'volumes': [{'name': m['Name'], 'target': m['Destination']}
                                for m in c.get('Mounts', []) if m['Type'] == 'volume']}
            logs = run(['docker','logs','--tail','100', c['Id']], check=False)
            item['startup_log'] = (logs.stdout + logs.stderr)[-16000:]
            found.append(item)
    print(redact(json.dumps({'n8n_diagnostics': found}, ensure_ascii=False, indent=2)))
    return found

if __name__ == '__main__':
    try: report()
    except (ValueError, OSError, subprocess.SubprocessError) as e: raise SystemExit('FAIL: '+redact(e))
