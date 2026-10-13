"""Live smoke test for the gated demo Deployment and Reconciler.
Uses Python stdlib, Docker CLI, and psql inside the existing postgres container.
No GPU, Kafka shutdown, modification of old jobs, or new Python packages.
"""
import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid


def uid(value):
    return str(uuid.UUID(value))


def command(args, *, stdin=None, timeout=20):
    result = subprocess.run(args, input=stdin, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'command failed')
    return result.stdout.strip()


def sql(statement):
    # Shell text is fixed. Credentials remain inside the postgres container.
    return command(['docker', 'exec', '-i', 'postgres', 'sh', '-c',
                    'exec psql -X -qAt -U "$POSTGRES_USER" -d model_operations -v ON_ERROR_STOP=1'],
                   stdin=statement)


def row(query):
    value = sql('SELECT row_to_json(t) FROM (' + query + ') t;')
    return json.loads(value) if value else None


def api(base, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data,
                                    headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'API HTTP {exc.code}: {exc.read().decode(errors="replace")}') from exc


def wait_for(label, getter, predicate, timeout):
    deadline = time.monotonic() + timeout
    last = None
    last_print = None
    while time.monotonic() < deadline:
        last = getter()
        brief = json.dumps(last, ensure_ascii=False, sort_keys=True)
        if brief != last_print:
            print(f'[{label}] {brief}', flush=True)
            last_print = brief
        if predicate(last):
            return last
        if last and last.get('status') in ('failed', 'cancelled'):
            raise RuntimeError(f'{label} ended unexpectedly: {brief}')
        time.sleep(2)
    raise RuntimeError(f'{label} timeout after {timeout}s; last={last}')


def submission_delivered(resource, operation_id):
    if resource is None:
        return False
    if resource.get('operation_id') != operation_id:
        raise RuntimeError('domain submission has an unexpected Operation ID; check service versions')
    state = resource.get('submission_status')
    if state == 'blocked':
        raise RuntimeError('Operation registration blocked: ' + str(resource.get('submission_error_code')))
    if state not in ('pending', 'delivered'):
        raise RuntimeError('domain submission status is unavailable; check service versions')
    return state == 'delivered'


def insert_fixture(operation_id, resource_id, execution_type, resource_type, kind):
    # Every interpolated string is either a validated UUID or a local constant.
    operation_id, resource_id = uid(operation_id), uid(resource_id)
    if (execution_type, resource_type, kind) not in (
        ('deployment.apply', 'deployment', 'smoke.reconcile.completed'),
        ('smoke.reconcile.running', 'smoke_fixture', 'smoke.reconcile.running'),
    ):
        raise ValueError('unsupported fixture')
    sql(f"""INSERT INTO operations
    (operation_id,kind,execution_type,resource_type,resource_id,status,phase,attempt,max_attempts,started_at,updated_at)
    VALUES ('{operation_id}','{kind}','{execution_type}','{resource_type}','{resource_id}',
            'dispatching','smoke_fixture',1,1,NOW()-INTERVAL '2 minutes',NOW()-INTERVAL '2 minutes');""")


def observation(operation_id):
    return row(f"SELECT operation_id,status,phase,reconcile_note FROM operations WHERE operation_id='{uid(operation_id)}'")


def resolve_images():
    # Use the exact running Operation image for the CPU sleep fixture. Never
    # depend on the removed all-services control tag or expose Compose env values.
    config = json.loads(command(['docker', 'compose', 'config', '--format', 'json']))
    deployment_image = config['services']['dispatcher']['environment']['DEPLOYMENT_JOB_IMAGE']
    sleep_image = command(['docker', 'inspect', '--format', '{{.Image}}', 'model-operation'])
    if not deployment_image or not sleep_image:
        raise RuntimeError('Operation and Deployment Job images must be configured')
    for image in (sleep_image, deployment_image):
        command(['docker', 'image', 'inspect', '--format', '{{.Id}}', image])
    return sleep_image, deployment_image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api', default='http://127.0.0.1:13000')
    parser.add_argument('--model-version-id', default='cceae526-7b7c-47d4-870b-117372b51885')
    parser.add_argument('--timeout', type=int, default=120)
    args = parser.parse_args()
    if args.timeout < 30 or args.timeout > 600:
        parser.error('--timeout must be 30..600')
    base = args.api.rstrip('/')
    model_id = uid(args.model_version_id)
    for container in ['postgres', 'model-operation', 'model-operation-dispatcher', 'model-operation-reconciler']:
        if command(['docker', 'inspect', '--format', '{{.State.Running}}', container]) != 'true':
            raise RuntimeError(f'{container} is not running')
    sleep_image, deployment_image = resolve_images()
    if command(['docker', 'image', 'inspect', '--format', '{{index .Config.Labels "model-platform.execution-gate"}}', deployment_image]) != '1':
        raise RuntimeError('deployment image is missing execution gate contract')
    model = api(base, '/api/model-versions/' + model_id)
    if model.get('state') != 'ready' or model.get('artifact_kind') != 'snapshot':
        raise RuntimeError('source model must be a ready snapshot')
    summary = {}
    fixture_ids = []
    container_name = None
    completed = False
    try:
        print('[1/3] Demo Deployment through REST -> Outbox -> Kafka -> Dispatcher (no GPU)', flush=True)
        # This legacy client sends no Idempotency-Key, so it must send only once.
        response = api(base, '/api/deployments', {'model_version_id': model_id, 'resources': {'gpu_count': 0}})
        op_id, dep_id = uid(response['operation_id']), uid(response['deployment_id'])
        summary.update(deployment_id=dep_id, delivery_operation_id=op_id)
        print(json.dumps(summary), flush=True)
        # 202 guarantees the domain's durable submission. Operation may still
        # return 404 until its registration is acknowledged after an outage.
        wait_for('submission', lambda: api(base, '/api/deployments/' + dep_id),
                 lambda v: submission_delivered(v, op_id), args.timeout)
        wait_for('delivery', lambda: api(base, '/api/operations/' + op_id),
                 lambda v: v and v.get('status') == 'succeeded', args.timeout)
        grant = row(f"SELECT dispatch_protocol,dispatch_generation,execution_started_at IS NOT NULL AS execution_granted,execution_instance_id IS NOT NULL AS process_recorded FROM operations WHERE operation_id='{op_id}'")
        if not grant or grant['dispatch_protocol'] != 1 or not grant['execution_granted'] or not grant['process_recorded']:
            raise RuntimeError('demo job did not pass the durable execution gate')
        print('[execution-gate] ' + json.dumps(grant, sort_keys=True), flush=True)
        published = wait_for('outbox', lambda: row(f"SELECT event_id,publish_attempts,published_at IS NOT NULL AS published FROM operation_outbox WHERE operation_id='{op_id}'"),
                             lambda v: v and v.get('published'), args.timeout)
        dep = api(base, '/api/deployments/' + dep_id)
        if dep.get('status') != 'ready':
            raise RuntimeError('demo deployment is not ready')
        summary['outbox_event_id'] = published['event_id']

        print('[2/3] Restore completion from the confirmed demo domain result', flush=True)
        terminal_id = str(uuid.uuid4())
        fixture_ids.append(terminal_id)
        summary['completion_fixture_id'] = terminal_id
        # No outbox row: only Reconciler observes this isolated synthetic record.
        insert_fixture(terminal_id, dep_id, 'deployment.apply', 'deployment', 'smoke.reconcile.completed')
        result = wait_for('completion-repair', lambda: observation(terminal_id),
                          lambda v: v and v.get('status') == 'succeeded', args.timeout)
        if result.get('phase') != 'reconciled_succeeded':
            raise RuntimeError('completion was not reported by Reconciler')

        print('[3/3] Restore running from an owned CPU-only sleep container', flush=True)
        running_id = str(uuid.uuid4())
        fixture_ids.append(running_id)
        summary['running_fixture_id'] = running_id
        container_name = 'model-op-' + running_id
        command(['docker', 'run', '-d', '--rm', '--name', container_name,
                 '--network', 'none', '--memory', '32m', '--cpus', '0.1',
                 '--label', 'model-platform.owner=operation-dispatcher',
                 '--label', 'model-platform.operation_id=' + running_id,
                 '--label', 'model-platform.execution_type=smoke.reconcile.running',
                 '--label', 'model-platform.smoke=true',
                 '--entrypoint', '/bin/sleep', sleep_image, str(args.timeout + 60)])
        insert_fixture(running_id, str(uuid.uuid4()), 'smoke.reconcile.running', 'smoke_fixture', 'smoke.reconcile.running')
        result = wait_for('running-repair', lambda: observation(running_id),
                          lambda v: v and v.get('status') == 'running', args.timeout)
        if result.get('phase') != 'reconciled_running':
            raise RuntimeError('running was not reported by Reconciler')
        completed = True
    finally:
        cleanup_errors = []
        # Only this invocation's known fixture UUIDs can be changed. Keep records
        # for diagnosis; cancel any fixture still active after the test.
        for operation_id in fixture_ids:
            try:
                sql(f"""UPDATE operations SET status='cancelled',phase='smoke_cleanup',completed_at=NOW(),updated_at=NOW()
                WHERE operation_id='{uid(operation_id)}' AND kind IN ('smoke.reconcile.completed','smoke.reconcile.running')
                  AND status IN ('dispatching','running');""")
            except Exception as exc:
                cleanup_errors.append(f'fixture {operation_id}: {exc}')
        if container_name:
            try:
                # Verify the fixture label before removing even the UUID-named container.
                check = subprocess.run(['docker', 'inspect', '--format', '{{index .Config.Labels "model-platform.smoke"}}', container_name], capture_output=True, text=True, timeout=15)
                if check.returncode == 0 and check.stdout.strip() != 'true':
                    raise RuntimeError('refusing to remove container without smoke label')
                if check.returncode and 'No such object' not in check.stderr and 'No such container' not in check.stderr:
                    raise RuntimeError('cannot verify fixture ownership: ' + check.stderr.strip())
                result = subprocess.run(['docker', 'rm', '-f', container_name], capture_output=True, text=True, timeout=15)
                if result.returncode and 'No such container' not in result.stderr:
                    cleanup_errors.append(f'container {container_name}: {result.stderr.strip()}')
            except Exception as exc:
                cleanup_errors.append(f'container {container_name}: {exc}')
        print('[summary] ' + json.dumps(summary, ensure_ascii=False), flush=True)
        if cleanup_errors:
            raise RuntimeError('cleanup incomplete: ' + '; '.join(cleanup_errors))
    if completed:
        print('PASS: real Kafka delivery and execution gate, legacy completion/running repair; fixture cleanup completed.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Interrupted; cleanup attempted.', flush=True)
        raise SystemExit(130)
    except Exception as exc:
        print('FAIL: ' + str(exc), flush=True)
        raise SystemExit(1)
