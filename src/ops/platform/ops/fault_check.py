#!/usr/bin/env python3
"""Run synthetic fault probes in new processes using current Ops/Agent image code.

No restart, new container, rebuild, live API request or production credential read.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from src.ops.platform.contracts import ReadFailure, require
from src.ops.platform.ops.restart_check import Docker
from src.ops.platform.transport import strict_json

SPECS = {'ops': ('agent-project-ops', 'ops-web', 'src/ops/platform'),
         'agent': ('agent-platform', 'agent-runtime', 'src/platform/agent/runtime/agent_runtime')}
INSPECT = '''{"id":{{json .Id}},"image":{{json .Image}},"started":{{json .State.StartedAt}},
"running":{{json .State.Running}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},
"project":{{json (index .Config.Labels "com.docker.compose.project")}},
"service":{{json (index .Config.Labels "com.docker.compose.service")}},"user":{{json .Config.User}}}'''


def hashes(kind, local=False):
    folder = ROOT / SPECS[kind][2]
    files = sorted(folder.glob('*.py'))
    if kind == 'ops': files += sorted((folder/'profiles').glob('*.json'))
    require(len(files) >= 5 and not folder.is_symlink(), 'SOURCE_FILES_MISSING')
    result = {}
    for path in files:
        require(not path.is_symlink() and path.is_file(), 'SOURCE_SYMLINK_REFUSED')
        destination = str(path) if local else '/app/' + folder.name + '/' + str(path.relative_to(folder))
        result[destination] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def program(kind, local=False):
    worker = Path(__file__).with_name('fault_probe.py')
    require(not worker.is_symlink() and worker.stat().st_size < 100000, 'INVALID_PROBE_WORKER')
    # JSON quoting constructs Python literals only for trusted local source and fixed paths.
    return ('import sys\nsys.dont_write_bytecode = True\n'
            'scope = {"__name__": "isolated_fault_probe"}\n'
            'exec(compile(' + repr(worker.read_text()) + ', "<isolated-fault-probe>", "exec"), scope)\n'
            'sys.exit(scope["main"](' + repr(kind) + ', ' + repr(hashes(kind, local)) + '))\n')


def parse_probe(completed, kind):
    require(len(completed.stdout) <= 128*1024, 'PROBE_OUTPUT_TOO_LARGE')
    try:
        value = strict_json(completed.stdout)
    except ReadFailure:
        raise ReadFailure('PROBE_OUTPUT_INVALID') from None
    require(isinstance(value, dict) and value.get('component') == kind, 'PROBE_OUTPUT_INVALID')
    if completed.returncode != 0 or value.get('result') != 'passed':
        code = value.get('code', '')
        require(isinstance(code, str) and re.fullmatch('[A-Z_]{1,80}', code), 'PROBE_FAILED')
        raise ReadFailure(code)
    expected = set(('http_503', 'http_401', 'invalid_json', 'timeout', 'connection_refused'))
    cases = value.get('cases')
    require(isinstance(cases, list) and all(isinstance(c, dict) and c.get('result') == 'passed' for c in cases), 'PROBE_CASES_INCOMPLETE')
    if kind == 'ops':
        require(len(cases) == 10 and {(c.get('source'), c.get('fault')) for c in cases}
                == {(s, f) for s in ('prometheus', 'langfuse') for f in expected}, 'PROBE_CASES_INCOMPLETE')
    else:
        require(len(cases) == 5 and {c.get('fault') for c in cases} == expected
                and value.get('queue_pressure', {}).get('result') == 'passed', 'PROBE_CASES_INCOMPLETE')
    require(value.get('synthetic_only') is True and value.get('production_api_requests') == 0
            and value.get('production_credentials_read') is False
            and value.get('source_hashes_verified') == len(hashes(kind)), 'PROBE_SCOPE_MISMATCH')
    return value


def identity(docker, kind):
    project, service, _ = SPECS[kind]
    ids = docker.command(['ps', '--no-trunc', '--filter', 'label=com.docker.compose.project=' + project,
        '--filter', 'label=com.docker.compose.service=' + service, '--format', '{{.ID}}']).split()
    require(len(ids) == 1 and re.fullmatch('[a-f0-9]{64}', ids[0]), 'EXPECTED_ONE_HEALTHY_CONTAINER')
    raw = strict_json(docker.command(['inspect', '--type', 'container', '--format', INSPECT, ids[0]]))
    require(isinstance(raw, dict) and raw.get('id') == ids[0]
            and raw.get('project') == project and raw.get('service') == service
            and raw.get('running') is True and raw.get('health') == 'healthy', 'CONTAINER_NOT_HEALTHY')
    require(re.fullmatch('sha256:[a-f0-9]{64}', raw.get('image', ''))
            and isinstance(raw.get('started'), str) and raw['started'], 'INVALID_CONTAINER_IDENTITY')
    expected_user = str(os.getuid()) + ':' + str(os.getgid()) if kind == 'ops' else '65532:65532'
    require(raw.get('user') == expected_user, 'CONTAINER_USER_MISMATCH')
    return raw


def invoke(args, payload, env):
    try:
        return subprocess.run(args, input=payload, text=True, capture_output=True, env=env, timeout=85)
    except subprocess.TimeoutExpired:
        # The worker has its own 75-second deadline; docker exec client is not a kill API.
        raise ReadFailure('PROBE_TIMEOUT') from None


def run(local=False):
    result = {'schema': 1, 'stage': 'O03-observation-fault-isolation',
              'scope': 'local_synthetic_processes' if local else 'deployed_images_isolated_synthetic_processes',
              'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
              'result': 'attention_required', 'components': {}, 'restart_requested': False,
              'new_container_requested': False, 'production_api_requests': 0, 'inference_requested': False,
              'browser_acceptance': 'deferred_by_user', 'live_backend_outage_or_database_acceptance': False}
    try:
        docker = None if local else Docker()
        before = {} if local else {kind: identity(docker, kind) for kind in SPECS}
        for kind in SPECS:
            if local:
                env = dict(os.environ)
                paths = [str(ROOT), str(ROOT/'src/platform/agent/runtime'), env.get('PYTHONPATH', '')]
                env['PYTHONPATH'] = os.pathsep.join(paths)
                args = [sys.executable, '-B', '-']
            else:
                env = docker.env
                args = ['docker', 'exec', '-i', before[kind]['id'], 'python3', '-B', '-']
            completed = invoke(args, program(kind, local), env)
            value = parse_probe(completed, kind)
            if not local:
                after = identity(docker, kind)
                require(before[kind] == after, 'CONTAINER_CHANGED_DURING_PROBE')
                value['container_id'] = after['id']
                value['image_id'] = after['image']
                value['same_container_and_started_at'] = True
            result['components'][kind] = value
        # Recheck both at the end: the Agent probe cannot hide an intervening Ops restart.
        if not local:
            require(all(identity(docker, k) == before[k] for k in SPECS), 'CONTAINER_CHANGED_DURING_PROBE')
        result['result'] = 'passed'
    except (ReadFailure, OSError) as exc:
        result['error'] = {'code': exc.code if isinstance(exc, ReadFailure) else 'LOCAL_IO_ERROR'}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local', action='store_true', help='developer mode: host dependencies, explicitly separate from image acceptance')
    args = parser.parse_args()
    result = run(args.local)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['result'] == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
