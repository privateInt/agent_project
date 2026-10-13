"""Read-only acceptance check using Docker's real Compose and Bake parsers.
Run from model_platform. Does not print resolved environment values or start jobs.
"""
import json
from pathlib import Path
import subprocess

SERVICES = ('gateway', 'management', 'operation', 'training', 'deployment', 'dispatcher', 'reconciler', 'gc')
CONTAINERS = {name: 'model-' + name for name in SERVICES}
CONTAINERS.update(dispatcher='model-operation-dispatcher', reconciler='model-operation-reconciler', gc='model-job-gc')
JOBS = {
    'management-import-job': 'model-platform-management-import-job:launch-v1',
    'training-job': 'model-platform-training-axolotl:launch-v1',
    'deployment-job': 'model-platform-deployment-job:launch-v1',
    'deployment-serving-job': 'model-platform-deployment-job:serving-v1',
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def parsed(args, label):
    result = subprocess.run(args, text=True, capture_output=True, timeout=60)
    # Compose output may contain credentials. Emit the failing check, not raw output.
    require(result.returncode == 0, label + ' failed; inspect this command locally for details')
    return json.loads(result.stdout)


def check():
    root = Path(__file__).resolve().parents[1]
    require(Path.cwd().resolve() == root, 'run from ~/workspace/agent_project/model_platform')
    config = parsed(['docker', 'compose', '--profile', 'playground', 'config', '--format', 'json'], 'Compose parsing')
    require(config.get('name') == 'model-platform-e2e', 'Compose project identity changed')
    require(set(config['services']) == set(SERVICES) | {'playground'}, 'expected eight control services and optional playground; no Job services')
    ui = config['services']['playground']
    require(ui.get('profiles') == ['playground'], 'Playground must remain opt-in')
    require(ui['image'] == 'model-platform-playground:dev' and ui['container_name'] == 'model-playground', 'Playground identity changed')
    require(Path(ui['build']['context']).resolve() == root and ui['build']['dockerfile'] == 'playground/Dockerfile', 'Playground build paths changed')
    require(ui['environment']['MODEL_GATEWAY_URL'] == 'http://gateway:8000', 'Playground must use Gateway internally')
    require(set(ui['networks']) == {'ops-infra'} and set(ui.get('depends_on', {})) == {'gateway'}, 'Playground network/dependency mismatch')
    require(not ui.get('volumes') and not ui.get('gpus') and not ui.get('devices'), 'Playground must not mount data, Docker socket or GPUs')
    require(set(ui['environment']) == {'MODEL_GATEWAY_URL', 'MODEL_PLAYGROUND_GPU_ID'}, 'unexpected Playground credentials/environment')
    require(config['networks']['ops-infra'].get('external') is True, 'shared network must remain external')
    for name in SERVICES:
        service = config['services'][name]
        require(service['container_name'] == CONTAINERS[name], name + ': container name changed')
        require(service['image'] == 'model-platform-' + name + ':dev', name + ': expected an independent image')
        require(service['build']['target'] == name, name + ': wrong Dockerfile target')
        context = Path(service['build']['context']).resolve()
        dockerfile = Path(service['build']['dockerfile'])
        require(context == root, name + ': include build context does not resolve to model_platform')
        require((context / dockerfile).resolve() == root / 'Dockerfile', name + ': wrong Dockerfile path')
        require(set(service['networks']) == {'ops-infra'}, name + ': unexpected networks')
        require(service['restart'] == 'unless-stopped', name + ': restart policy changed')
        require(service['command'] == ['/app/' + name], name + ': wrong executable')
        require(set(service.get('depends_on', {})).issubset(SERVICES), name + ': missing sibling dependency')
        environment = service.get('environment', {})
        for key in ('POSTGRES_URL', 'CREDENTIAL_ENCRYPTION_KEY', 'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY'):
            if key in environment:
                require(bool(environment[key]), name + ': ' + key + ' did not resolve from the root environment')
        for value in environment.values():
            require('${' not in str(value), name + ': unresolved environment substitution')
        mounts = service.get('volumes', [])
        sockets = [v for v in mounts if v.get('target') == '/var/run/docker.sock']
        require(bool(sockets) == (name in ('training', 'deployment', 'dispatcher', 'reconciler', 'gc')), name + ': unexpected Docker socket exposure')
    deployment = config['services']['deployment']
    gpu_volumes = []
    for name in ('dispatcher', 'deployment', 'training'):
        service = config['services'][name]
        admission = [v for v in service.get('volumes', []) if v.get('target') == '/gpu-admission']
        require(len(admission) == 1 and admission[0]['type'] == 'volume', name + ': GPU admission volume missing')
        require(service['environment'].get('GPU_ADMISSION_DIR') == '/gpu-admission', name + ': GPU lock path mismatch')
        gpu_volumes.append(config['volumes'][admission[0]['source']]['name'])
    require(gpu_volumes == ['model-gpu-admission'] * 3, 'GPU creators and canceller must share the same durable admission volume')
    cache = [v for v in deployment['volumes'] if v.get('target') == '/serving-cache']
    require(len(cache) == 1 and cache[0]['type'] == 'volume', 'Deployment serving cache volume is missing')
    require(config['volumes'][cache[0]['source']]['name'] == deployment['environment']['SERVING_CACHE_VOLUME'],
            'controller and vLLM must use the same Docker cache volume')
    gc_env = config['services']['gc']['environment']
    require(config['services']['training']['environment'].get('GC_LOG_BUCKET') == gc_env.get('GC_LOG_BUCKET'),
            'Training must read the GC archive bucket')
    require(gc_env.get('GC_MODE') in ('dry-run', 'archive', 'apply'), 'GC mode is invalid')
    require(gc_env.get('GC_LOG_BUCKET') != gc_env.get('MINIO_BUCKET'), 'GC logs must use a separate bucket')
    output_mounts = [v for v in config['services']['training'].get('volumes', []) if v.get('target') == '/training-outputs']
    require(len(output_mounts) == 1 and output_mounts[0]['type'] == 'volume', 'Training durable output volume missing')
    output_volume = config['volumes'][output_mounts[0]['source']]['name']
    require(output_volume == config['services']['dispatcher']['environment'].get('TRAINING_OUTPUT_VOLUME'),
            'Dispatcher Jobs and Training recovery must share the output volume')
    require(config['services']['training']['environment'].get('TRAINING_OUTPUT_ROOT') == '/training-outputs', 'Training output path mismatch')
    job_env = config['services']['dispatcher']['environment']
    for key, target in [('MANAGEMENT_IMPORT_JOB_IMAGE', 'management-import-job'),
                        ('TRAINING_JOB_IMAGE', 'training-job'), ('DEPLOYMENT_JOB_IMAGE', 'deployment-job'),
                        ('DEPLOYMENT_SERVING_JOB_IMAGE', 'deployment-serving-job')]:
        require(job_env.get(key) == JOBS[target], 'Dispatcher Job image differs from its build tag')

    graph = parsed(['docker', 'buildx', 'bake', '--builder', 'default', '-f', 'docker-bake.hcl',
                    '--print', 'jobs', 'check', 'dispatch-fault'], 'Bake graph parsing')['target']
    for target, tag in JOBS.items():
        require(graph[target].get('tags') == [tag], target + ': incorrect image tag')
    require(graph['training-job']['dockerfile'] == 'jobs/training/axolotl/Dockerfile', 'Axolotl Dockerfile missing')
    require(graph['training-job']['contexts'].get('training_binary') == 'target:training-binary',
            'Axolotl must consume the current source build, not an old local image')
    checkpoint_dockerfile = (root / 'jobs/training/axolotl/Dockerfile').read_text()
    for script in ('checkpoint_store.py', 'checkpoint_train.py', 'evaluation_data.py', 'evaluation_config.py', 'evaluation_train.py'):
        require('COPY jobs/training/axolotl/' + script + ' /app/' + script in checkpoint_dockerfile, 'Checkpoint Job helper missing: ' + script)
    require('model-platform.training-checkpoints="1"' in checkpoint_dockerfile, 'Checkpoint image capability missing')
    require('model-platform.training-evaluation="1"' in checkpoint_dockerfile, 'Evaluation image capability missing')
    require(graph['check']['target'] == 'builder', 'test builder target missing')
    require(graph['dispatch-fault']['contexts'] == {
        'go_builder': 'target:check', 'docker_runtime': 'target:docker-runtime'},
        'fault image must use the current source build graph')
    print('PASS: eight control images and optional Streamlit UI, include paths/env/network, Job routing and current-source build dependencies.')


if __name__ == '__main__':
    try:
        check()
    except (KeyError, ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise SystemExit('FAIL: ' + str(exc)) from None
