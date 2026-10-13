"""Probe an inherited container HOSTNAME before repairing only Web/Worker binding."""
import json
from pathlib import Path

import manage
from lf_common import DockerFailure, SERVICES, require

APPS = ('lf-worker', 'lf-web')
STORES = tuple(x for x in SERVICES if x not in APPS)
ENDPOINTS = {
    'lf-worker': (3030, [('health', '/api/health?failIfEventPropagationStuck=true')]),
    'lf-web': (3000, [('ready', '/api/public/ready'),
                      ('health', '/api/public/health?failIfDatabaseUnavailable=true')]),
}
# Only status/error codes leave the container. Never print response bodies, URLs or env.
PROBE = r"""
const [port, endpoints] = JSON.parse(process.argv[1]);
const configured = process.env.HOSTNAME;
const os = require('node:os');
const wildcard = configured === '0.0.0.0';
if (!wildcard && configured !== os.hostname()) process.exit(2);
const hosts = [['loopback','127.0.0.1'], ['configured', wildcard ? '127.0.0.1' : configured]];
Promise.all(hosts.flatMap(([address,host]) => endpoints.map(async([endpoint,path]) => {
  try {
    const r = await fetch('http://' + host + ':' + port + path,
      {signal:AbortSignal.timeout(4000),redirect:'error'});
    await r.body?.cancel();
    return {address,endpoint,status:r.status};
  } catch(e) {
    const code = e.cause?.code || e.name;
    return {address,endpoint,error:['ECONNREFUSED','ETIMEDOUT','ENOTFOUND','EHOSTUNREACH',
      'TimeoutError','AbortError'].includes(code) ? code : 'REQUEST_FAILED'};
  }
}))).then(rows => {console.log(JSON.stringify(rows));process.exit(0);})
  .catch(() => process.exit(2));
"""


def inventory(docker, settings):
    items = manage.owned_containers(docker, settings['owner'])
    require(len(items) == len(SERVICES), '기존 Langfuse 서비스 6개가 필요합니다. diagnose로 확인하세요.')
    for item in items:
        manage.inspect_binding(item, settings)
    return {x['Config']['Labels']['com.docker.compose.service']: x for x in items}


def hostname(item):
    values = [x.split('=', 1)[1] for x in item['Config'].get('Env', []) if x.startswith('HOSTNAME=')]
    # Docker injects its default HOSTNAME at runtime; Config.Env may omit it.
    inherited = item['Config'].get('Hostname')
    require(isinstance(inherited, str) and inherited and len(values) <= 1,
            '컨테이너 HOSTNAME을 확인하지 못했습니다.')
    value = values[0] if values else inherited
    require(value in ('0.0.0.0', inherited), '예상과 다른 HOSTNAME입니다. 자동 수정하지 않습니다.')
    return value


def probe(docker, service, item):
    require(item['State']['Running'], service + '가 실행 중이 아닙니다. 자동 수정하지 않습니다.')
    configured = hostname(item)
    rows = docker.json(['exec', item['Id'], 'node', '-e', PROBE, json.dumps(ENDPOINTS[service])])
    expected = {(address, label) for address in ('loopback', 'configured') for label, _ in ENDPOINTS[service][1]}
    require(isinstance(rows, list) and len(rows) == len(expected), 'HTTP probe 응답 개수가 다릅니다.')
    seen = set()
    for row in rows:
        require(isinstance(row, dict) and set(row) in ({'address', 'endpoint', 'status'}, {'address', 'endpoint', 'error'}),
                'HTTP probe 응답 형식이 다릅니다.')
        seen.add((row['address'], row['endpoint']))
        require(('status' in row and type(row['status']) is int and 100 <= row['status'] <= 599)
                or row.get('error') in ('ECONNREFUSED', 'ETIMEDOUT', 'ENOTFOUND', 'EHOSTUNREACH',
                                       'TimeoutError', 'AbortError', 'REQUEST_FAILED'), 'HTTP probe 상태가 다릅니다.')
    require(seen == expected, 'HTTP probe endpoint 구성이 다릅니다.')
    loopback = [x for x in rows if x['address'] == 'loopback']
    other = [x for x in rows if x['address'] == 'configured']
    mismatch = configured != '0.0.0.0' and all(x.get('error') == 'ECONNREFUSED' for x in loopback) and all(x.get('status') == 200 for x in other)
    ready = configured == '0.0.0.0' and all(x.get('status') == 200 for x in rows)
    return {'binding': 'wildcard' if configured == '0.0.0.0' else 'container_hostname',
            'http': rows, 'mismatch_confirmed': mismatch, 'binding_ready': ready}


def store_snapshot(items):
    return {key: {field: items[key].get(field) for field in ('Id', 'Image', 'Mounts')} |
            {'started_at': items[key]['State'].get('StartedAt'), 'restarts': items[key].get('RestartCount')}
            for key in STORES}


def repair(docker, settings, wait_seconds=120):
    root = Path(settings['state_dir'])
    manage.load(root)  # All existing private settings, secrets and directory inode bindings.
    manage.check_networks(docker, settings['owner'])
    before = inventory(docker, settings)
    require(all(before[x]['State']['Running'] and before[x]['State'].get('Health', {}).get('Status') == 'healthy'
                for x in STORES), '저장소 4개의 Healthy가 필요합니다. 자동 수정하지 않습니다.')
    config = docker.json(manage.compose(settings, 'config', '--format', 'json'))
    manage.config_contract(config, settings)
    probes = {key: probe(docker, key, before[key]) for key in APPS}
    print(json.dumps({'stage': 'O01-Langfuse-health-probe', 'services': probes,
                      'changes_made': False, 'inference_executed': False}, ensure_ascii=False, indent=2), flush=True)
    require(all(x['mismatch_confirmed'] or x['binding_ready'] for x in probes.values()),
            '확인된 바인딩 불일치가 아닙니다. 서비스를 변경하지 않았습니다. diagnose 출력을 확인하세요.')
    targets = [key for key in APPS if probes[key]['mismatch_confirmed']]
    snapshot = store_snapshot(before)
    frozen = {name: (root / name).read_bytes() for name in ('settings.json', 'owner.json', *manage.environments(settings))}
    try:
        if targets:
            print('주소 불일치를 확인했습니다. 해당 Web/Worker만 같은 이미지로 재생성합니다.', flush=True)
            docker.run(manage.compose(settings, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never',
                                      '--wait', '--wait-timeout', str(max(30, wait_seconds)), *targets),
                       timeout=max(30, wait_seconds) + 30)
        result = manage.check(docker, settings, wait_seconds)
    except DockerFailure as exc:
        from diagnose import report
        print(json.dumps(report(docker, root, failure=exc), ensure_ascii=False, indent=2), flush=True)
        raise
    after = inventory(docker, settings)
    require(store_snapshot(after) == snapshot, '검사 중 저장소 컨테이너/이미지/저장 연결/시작 시간이 달라졌습니다.')
    require(all((root / name).read_bytes() == value for name, value in frozen.items()), '검사 중 비공개 설정이 달라졌습니다.')
    manage.load(root)
    require(all(hostname(after[key]) == '0.0.0.0' for key in APPS), '실제 Web/Worker 바인딩 설정을 확인하지 못했습니다.')
    result.update(stage='O01-Langfuse-health-repair', result='passed', repaired_services=targets,
                  changes_made=bool(targets), storage_containers_unchanged=True,
                  settings_and_secrets_preserved=True, images_pulled=False, domain_services_restarted=False,
                  trace_delivery_verified=False, inference_executed=False)
    return result
