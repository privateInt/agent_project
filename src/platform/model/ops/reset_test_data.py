"""Reset only the prepared local model-platform test resources. No data backup."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from common import Context, DATABASES, OpsError, OWNERS, ROOT, containers, docker, private_json, require
from backup import s3_client, inventory as s3_inventory

TABLES = {
    'model_operations': ('operations', 'operation_outbox'),
    'model_management': ('models', 'model_versions', 'import_credentials', 'api_requests', 'operation_submissions'),
    'model_training': ('training_runs', 'datasets', 'dataset_versions', 'training_output_recoveries', 'api_requests', 'operation_submissions'),
    'model_deployment': ('deployments', 'api_requests', 'operation_submissions'),
}
# Observed on the upgraded test server. Fresh installs do not create this table.
# Its rows are reset, but its IDs never authorize current jobs or S3 objects.
OPTIONAL_TABLES = {'model_management': ('operations',)}
KINDS = ('training-outputs', 'serving-cache', 'gpu-admission')
UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
FULL_ID = r'[0-9a-f]{64}'


def hash_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def json_rows(ctx, db, query):
    return json.loads(ctx.sql(db, 'SELECT COALESCE(json_agg(row_to_json(r)),\'[]\'::json) FROM (' + query + ') r'))


def checked_tables(db, actual):
    require(db in TABLES, 'Unexpected database')
    required = {('public', t) for t in TABLES[db]}
    allowed = required | {('public', t) for t in OPTIONAL_TABLES.get(db, ())}
    missing, unexpected = required - actual, actual - allowed
    require(not missing and not unexpected,
            'Unexpected schema/table in ' + db + ': ' + json.dumps({
                'missing': ['.'.join(t) for t in sorted(missing)],
                'unexpected': ['.'.join(t) for t in sorted(unexpected)],
            }))
    return sorted(t for schema, t in actual)


def db_inventory(ctx):
    result = {}
    for db in TABLES:
        tables = json_rows(ctx, db, "SELECT schemaname,tablename FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema') AND schemaname NOT LIKE 'pg_toast%' ORDER BY schemaname,tablename")
        selected = checked_tables(db, {(r['schemaname'], r['tablename']) for r in tables})
        require(ctx.sql(db, "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal") == '0', 'User trigger in ' + db)
        counts = {}
        for table in selected:
            row = json_rows(ctx, db, 'SELECT count(*) AS count, md5(COALESCE(string_agg(md5(t::text),\'\' ORDER BY md5(t::text)),\'\')) AS digest FROM public."' + table + '" t')[0]
            counts[table] = row
        result[db] = counts
    return result


def identities(ctx):
    operations = json_rows(ctx, 'model_operations', 'SELECT operation_id::text,resource_id::text,execution_type FROM operations ORDER BY operation_id')
    models = json_rows(ctx, 'model_management', 'SELECT model_id::text,model_version_id::text FROM model_versions ORDER BY model_version_id')
    training = json_rows(ctx, 'model_training', 'SELECT training_run_id::text,mlflow_run_id,mlflow_experiment_id FROM training_runs ORDER BY training_run_id')
    deployments = json_rows(ctx, 'model_deployment', 'SELECT deployment_id::text FROM deployments ORDER BY deployment_id')
    datasets = json_rows(ctx, 'model_training', 'SELECT dataset_id::text FROM datasets ORDER BY dataset_id')
    return {'operations': operations, 'model_versions': models, 'training_runs': training,
            'deployments': deployments, 'datasets': datasets}


def owned_jobs(items, ids, node):
    operations = {r['operation_id']: r for r in ids['operations']}
    deployments = {r['deployment_id']: r for r in ids['deployments']}
    result = []
    for c in items:
        labels = c['Config'].get('Labels') or {}
        owner = labels.get('model-platform.owner')
        name = c['Name'].lstrip('/')
        if owner not in OWNERS:
            require(not name.startswith(('model-op-', 'model-serving-')), 'Unrecognized model runtime owner: ' + name)
            continue
        op = operations.get(labels.get('model-platform.operation_id'))
        require(op is not None and re.fullmatch(FULL_ID, c['Id']), 'Runtime is not linked to a platform Operation')
        if owner == 'operation-dispatcher':
            token = labels.get('model-platform.dispatch_token', '')
            require(re.fullmatch(UUID, token) and name == 'model-op-' + op['operation_id'] + '-' + token
                    and labels.get('model-platform.execution_type') == op['execution_type'], 'Job identity mismatch')
            require(labels.get('model-platform.execution_node', '') in ('', node)
                    and labels.get('model-platform.requested_node', '') in ('', node), 'Nonlocal Job')
        else:
            dep = deployments.get(labels.get('model-platform.deployment_id'))
            require(dep is not None and dep['deployment_id'] == op['resource_id'] and op['execution_type'] == 'deployment.serve'
                    and name == 'model-serving-' + dep['deployment_id'], 'Serving identity mismatch')
        result.append({'id': c['Id'], 'name': name, 'owner': owner, 'operation_id': op['operation_id'],
                       'image': c['Image'], 'labels_sha256': hash_json(labels)})
    return sorted(result, key=lambda r: r['id'])


def inactive(c):
    return c['State'].get('Status') in ('exited', 'created') and not any(c['State'].get(k) for k in ('Running', 'Paused', 'Restarting'))


def check_mounts(ctx, items, jobs):
    accepted = {c['Id'] for c in ctx.services.values()} | {r['id'] for r in jobs}
    targets = {ctx.volumes[k] for k in KINDS}
    for c in items:
        if any(m.get('Name') in targets for m in c.get('Mounts', [])):
            require(c['Id'] in accepted, 'Another container uses a reset volume: ' + c['Name'])


def cold(ctx, jobs):
    now = containers()
    by_id = {c['Id']: c for c in now}
    for c in ctx.services.values():
        require(c['Id'] in by_id and inactive(by_id[c['Id']]), 'Platform services must remain stopped')
    known = {r['id'] for r in jobs}
    for c in now:
        if (c['Config'].get('Labels') or {}).get('model-platform.owner') in OWNERS:
            require(c['Id'] in known and inactive(c), 'New or active model Job/runtime during reset')
    check_mounts(ctx, now, jobs)
    for db in DATABASES:
        require(ctx.sql(db, 'SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()') == '0',
                'Another PostgreSQL session is connected to ' + db)


def endpoint(item, port):
    ports = item.get('NetworkSettings', {}).get('Ports', {}).get(str(port) + '/tcp') or []
    candidates = {r['HostPort'] for r in ports if r['HostIp'] in ('0.0.0.0', '127.0.0.1')}
    require(len(candidates) == 1, 'Expected one host-reachable local API port for ' + item['Name'])
    value = next(iter(candidates))
    require(value.isdigit() and 0 < int(value) < 65536, 'Invalid host port')
    return 'http://127.0.0.1:' + value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http(base, path, body=None, missing=False):
    parsed = urlsplit(base)
    require(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and not parsed.username and not parsed.password,
            'Local API endpoint required')
    req = Request(base + path, data=None if body is None else json.dumps(body).encode(),
                  headers={'Content-Type': 'application/json'}, method='GET' if body is None else 'POST')
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        # MLflow may return 404 or 400 for RESOURCE_DOES_NOT_EXIST.
        raw = exc.read(65536)
        try:
            error = json.loads(raw)
        except ValueError:
            error = {}
        if missing and exc.code in (400, 404) and error.get('error_code') == 'RESOURCE_DOES_NOT_EXIST':
            return None
        raise OpsError('Local API request failed (status ' + str(exc.code) + ')') from None


class Backend:
    def __init__(self, ctx):
        self.ctx = ctx
        env = ctx.config['services']['training']['environment']
        require(len(set(ctx.volumes.values())) == 4, 'Reset volumes must be distinct from each other and MLflow')
        self.buckets = {'models': env['MINIO_BUCKET'], 'datasets': env['DATASET_BUCKET'], 'logs': env['GC_LOG_BUCKET']}
        require(len(set(self.buckets.values())) == 3 and all(re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', b) for b in self.buckets.values()), 'Three distinct platform buckets required')
        self.minio = ctx.by_name.get('ops-minio1')
        self.kafka = ctx.by_name.get('ops-kafka')
        require(self.minio and self.kafka, 'Local MinIO and Kafka required')
        for c in (self.minio, self.kafka, ctx.pg, ctx.mlflow):
            require(c['State'].get('Running') and not c['State'].get('Restarting'), 'Shared infrastructure must be running')
        require(env['MINIO_ENDPOINT'] == 'ops-minio1:9000' and env.get('MINIO_USE_SSL', 'false') == 'false', 'Only current local MinIO layout supported')
        self.s3_endpoint = endpoint(self.minio, 9000)
        self.client = s3_client(self.s3_endpoint, env['MINIO_ACCESS_KEY'], env['MINIO_SECRET_KEY'])
        require(env['MLFLOW_TRACKING_URI'] == 'http://ops-mlflow:5000', 'Only local MLflow supported')
        self.mlflow_endpoint = endpoint(ctx.mlflow, 5000)
        dispatcher = ctx.config['services']['dispatcher']['environment']
        operation = ctx.config['services']['operation']['environment']
        self.topic = dispatcher['KAFKA_COMMAND_TOPIC']
        self.group = dispatcher['KAFKA_CONSUMER_GROUP']
        require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,200}', self.topic)
                and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,200}', self.group), 'Invalid topic/group')
        require(operation['KAFKA_COMMAND_TOPIC'] == self.topic
                and operation['KAFKA_BROKERS'] == dispatcher['KAFKA_BROKERS'] == 'ops-kafka:19092', 'Kafka configuration mismatch')
        self.node = dispatcher.get('DISPATCHER_NODE_NAME', '')

    def binding(self):
        return {'project': self.ctx.config['name'], 'config_sha256': hash_json(self.ctx.config),
                'services': {s: c['Id'] for s, c in self.ctx.services.items()},
                'postgres_id': self.ctx.pg['Id'], 'minio_id': self.minio['Id'], 'kafka_id': self.kafka['Id'],
                'mlflow_id': self.ctx.mlflow['Id'], 'volumes': {k: self.ctx.volumes[k] for k in KINDS},
                'buckets': self.buckets, 'topic': self.topic, 'consumer_group': self.group,
                'volume_identity': {k: {field: info.get(field) for field in ('Name', 'CreatedAt', 'Driver', 'Mountpoint')}
                                    for k in KINDS for info in json.loads(docker('volume', 'inspect', self.ctx.volumes[k]))}}

    def kcmd(self, command, *args, data=None):
        return docker('exec', '-i', self.kafka['Id'], '/opt/kafka/bin/' + command + '.sh',
                      '--bootstrap-server', 'localhost:19092', *args, data=data, timeout=120).decode()

    def offsets(self, time_value):
        text = self.kcmd('kafka-get-offsets', '--topic', '^' + re.escape(self.topic) + '$', '--time', str(time_value))
        rows = {}
        for line in text.splitlines():
            m = re.fullmatch(re.escape(self.topic) + r':(\d+):(\d+)', line.strip())
            if m:
                rows[m[1]] = int(m[2])
        require(rows, 'No Kafka partition offsets returned')
        return rows

    def kafka_inventory(self):
        described = self.kcmd('kafka-topics', '--describe', '--topic', self.topic)
        m = re.search(r'\bTopicId:\s+(\S+)', described)
        require(m is not None, 'Kafka topic identity unavailable')
        groups = self.kcmd('kafka-consumer-groups', '--all-groups', '--describe')
        for line in groups.splitlines():
            cols = line.split()
            if len(cols) >= 3 and cols[1] == self.topic:
                require(cols[0] == self.group, 'Another consumer group uses the platform topic')
        group_names = self.kcmd('kafka-consumer-groups', '--list').splitlines()
        if self.group in (name.strip() for name in group_names):
            state = self.kcmd('kafka-consumer-groups', '--group', self.group, '--describe', '--state')
            lines = [line for line in state.splitlines() if line.split() and line.split()[0] == self.group]
            require(len(lines) == 1 and re.search(r'\b(Empty|Dead)\s+0\s*$', lines[0]),
                    'Kafka consumer group is still active; wait for membership expiry, then repeat preparation')
        return {'topic_id': m[1], 'end': self.offsets(-1)}

    def clear_kafka(self, planned):
        require(self.kafka_inventory() == planned, 'Kafka topic changed after preparation')
        rows = [{'topic': self.topic, 'partition': int(k), 'offset': v} for k, v in planned['end'].items()]
        self.kcmd('kafka-delete-records', '--offset-json-file', '/dev/stdin', data=json.dumps({'partitions': rows, 'version': 1}).encode())
        require(self.offsets(-2) == planned['end'] == self.offsets(-1), 'Kafka old records remain readable')
        result = self.kcmd('kafka-consumer-groups', '--group', self.group, '--topic', self.topic,
                           '--reset-offsets', '--to-latest', '--execute')
        actual = {}
        for line in result.splitlines():
            cols = line.split()
            if len(cols) == 4 and cols[0] == self.group and cols[1] == self.topic and cols[2].isdigit() and cols[3].isdigit():
                actual[cols[2]] = int(cols[3])
        require(actual == planned['end'], 'Kafka group offset reset was not confirmed; keep Dispatcher stopped')
        return {'topic': self.topic, 'group': self.group, 'offsets': actual, 'old_records_readable': False}

    def objects(self, ids):
        result = {}
        for kind, bucket in self.buckets.items():
            listed = s3_inventory(self.client, bucket)
            for row in listed:
                require(valid_key(kind, row['key'], ids), 'Unrecognized object in platform bucket ' + bucket)
            uploads = []
            for page in self.client.get_paginator('list_multipart_uploads').paginate(Bucket=bucket):
                for row in page.get('Uploads', []):
                    require(valid_key(kind, row['Key'], ids), 'Unrecognized multipart upload in ' + bucket)
                    uploads.append({'key': row['Key'], 'upload_id': row['UploadId']})
            result[kind] = {'objects': listed, 'uploads': sorted(uploads, key=lambda r: (r['key'], r['upload_id']))}
        return result

    def clear_objects(self, planned, ids, guard):
        current = self.objects(ids)
        result = {}
        for kind, bucket in self.buckets.items():
            expected = {r['key']: r for r in planned[kind]['objects']}
            require(all(expected.get(r['key']) == r for r in current[kind]['objects']), 'S3 objects changed after preparation')
            require(all(r in planned[kind]['uploads'] for r in current[kind]['uploads']), 'New multipart upload')
            for upload in current[kind]['uploads']:
                guard()
                self.client.abort_multipart_upload(Bucket=bucket, Key=upload['key'], UploadId=upload['upload_id'])
            rows = current[kind]['objects']
            for start in range(0, len(rows), 500):
                guard()
                response = self.client.delete_objects(Bucket=bucket, Delete={'Objects': [{'Key': r['key']} for r in rows[start:start + 500]], 'Quiet': True})
                require(not response.get('Errors'), 'One or more S3 object deletions failed in ' + bucket)
            result[bucket] = {'deleted_objects': len(rows), 'logical_bytes': sum(r['size'] for r in rows)}
        require(all(not d['objects'] and not d['uploads'] for d in self.objects(ids).values()), 'S3 reset is incomplete')
        return result

    def volume(self, kind, ids, mode='inventory', entries=None):
        allowed = volume_ids(kind, ids)
        name = 'model-reset-helper-' + uuid.uuid4().hex
        volume = self.ctx.volumes[kind]
        docker('volume', 'inspect', volume)
        body = {'mode': mode, 'kind': kind, 'allowed_ids': allowed, 'entries': entries}
        try:
            raw = docker('run', '--rm', '-i', '--pull=never', '--name', name, '--network', 'none', '--read-only',
                         '--cap-drop', 'ALL', '--cap-add', 'DAC_OVERRIDE', '--security-opt', 'no-new-privileges', '--user', '0:0',
                         '--mount', 'type=volume,src=' + volume + ',dst=/data' + (',readonly' if mode == 'inventory' else ''),
                         '--entrypoint', 'python', self.ctx.helper_image, '-c', (ROOT / 'ops/reset_volume.py').read_text(),
                         data=json.dumps(body).encode(), timeout=600)
            return json.loads(raw)
        finally:
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)

    def mlflow_runs(self, ids):
        experiments = {r['mlflow_experiment_id'] for r in ids['training_runs'] if r['mlflow_experiment_id']}
        expected = {r['mlflow_run_id']: r for r in ids['training_runs'] if r['mlflow_run_id']}
        dispatcher = self.ctx.config['services']['dispatcher']['environment']
        named = http(self.mlflow_endpoint, '/api/2.0/mlflow/experiments/get-by-name?' + urlencode({'experiment_name': dispatcher['MLFLOW_EXPERIMENT_NAME']}), missing=True)
        if named:
            experiments.add(named['experiment']['experiment_id'])
        result = {}
        for exp in sorted(experiments):
            token = None
            seen = set()
            while True:
                body = {'experiment_ids': [exp], 'run_view_type': 'ALL', 'max_results': 1000}
                if token:
                    body['page_token'] = token
                page = http(self.mlflow_endpoint, '/api/2.0/mlflow/runs/search', body)
                for run in page.get('runs', []):
                    info = run['info']; tags = {t['key']: t['value'] for t in run.get('data', {}).get('tags', [])}
                    if matches_run(info, tags, ids):
                        result[info['run_id']] = {'run_id': info['run_id'], 'experiment_id': info['experiment_id'],
                                                   'training_run_id': tags['src.platform.model.training_run_id'],
                                                   'operation_id': tags['src.platform.model.operation_id']}
                    else:
                        require(info['run_id'] not in expected, 'Linked MLflow run lacks matching platform ownership tags')
                token = page.get('next_page_token')
                if not token:
                    break
                require(token not in seen, 'Repeated MLflow page token')
                seen.add(token)
        for run_id in expected.keys() - result.keys():
            require(http(self.mlflow_endpoint, '/api/2.0/mlflow/runs/get?' + urlencode({'run_id': run_id}), missing=True) is None,
                    'Linked MLflow run was not resolved')
        return sorted(result.values(), key=lambda r: r['run_id'])

    def clear_mlflow(self, planned, ids, guard):
        current = self.mlflow_runs(ids)
        require(all(r in planned for r in current), 'New or changed MLflow run after preparation')
        for row in current:
            guard()
            path = '/api/2.0/mlflow/runs/get?' + urlencode({'run_id': row['run_id']})
            got = http(self.mlflow_endpoint, path, missing=True)
            if not got:
                continue
            tags = {t['key']: t['value'] for t in got['run'].get('data', {}).get('tags', [])}
            require(matches_run(got['run']['info'], tags, ids), 'MLflow run ownership changed')
            if got['run']['info'].get('lifecycle_stage') != 'deleted':
                http(self.mlflow_endpoint, '/api/2.0/mlflow/runs/delete', {'run_id': row['run_id']})
            check = http(self.mlflow_endpoint, path)
            require(check['run']['info']['lifecycle_stage'] == 'deleted', 'MLflow run deletion not confirmed')
        return {'deleted_runs': [r['run_id'] for r in current], 'mode': 'soft-delete',
                'note': 'Shared MLflow SQLite and tombstones retained; no global mlflow gc'}


def valid_key(kind, key, ids):
    parts = key.split('/')
    if any(p in ('', '.', '..') for p in parts) or '\\' in key:
        return False
    if kind == 'models':
        pairs = {(r['model_id'], r['model_version_id']) for r in ids['model_versions']}
        return len(parts) >= 5 and parts[0] == 'models' and parts[2] == 'versions' and (parts[1], parts[3]) in pairs
    if kind == 'datasets':
        return len(parts) == 5 and parts[0] == 'datasets' and parts[1] in {r['dataset_id'] for r in ids['datasets']} and parts[2] == 'sha256' and bool(re.fullmatch(FULL_ID, parts[3])) and parts[4] == 'train.jsonl'
    if kind == 'logs':
        return len(parts) == 6 and parts[0] == 'operations' and parts[1] in {r['operation_id'] for r in ids['operations']} and bool(re.fullmatch(UUID, parts[2])) and bool(re.fullmatch(FULL_ID, parts[3])) and bool(re.fullmatch(FULL_ID, parts[4])) and parts[5] in ('manifest.json', 'metadata.json', 'stdout.log', 'stderr.log')
    return False


def matches_run(info, tags, ids):
    op = next((r for r in ids['operations'] if r['operation_id'] == tags.get('src.platform.model.operation_id')), None)
    training = {r['training_run_id'] for r in ids['training_runs']}
    return bool(re.fullmatch(r'[0-9a-f]{32}', info.get('run_id', '')) and op
                and op['execution_type'] == 'training.run' and op['resource_id'] == tags.get('src.platform.model.training_run_id')
                and op['resource_id'] in training)


def volume_ids(kind, ids):
    category, field = {'training-outputs': ('training_runs', 'training_run_id'),
                       'serving-cache': ('deployments', 'deployment_id'), 'gpu-admission': ('operations', 'operation_id')}[kind]
    return [r[field] for r in ids[category]]


def subset_state(current, planned):
    require(set(current) == set(planned) == set(TABLES), 'Database set changed')
    for db, tables in current.items():
        checked_tables(db, {('public', t) for t in tables})
        checked_tables(db, {('public', t) for t in planned[db]})
        require(set(tables) == set(planned[db]), 'Database table set changed after preparation: ' + db)
        require(tables == planned[db] or all(row['count'] == 0 for row in tables.values()), 'Database rows changed after preparation: ' + db)


def prepare(ctx, backend, plan_path):
    # Resolve ownership before stopping any resource; all IDs are reloaded after writers stop.
    ids = identities(ctx)
    jobs = owned_jobs(containers(), ids, backend.node)
    check_mounts(ctx, containers(), jobs)
    db_inventory(ctx)
    for names in (('playground', 'gateway', 'dispatcher'), ('gc', 'reconciler', 'deployment', 'training', 'management', 'operation')):
        docker('stop', '--time', '20', *(ctx.services[n]['Id'] for n in names), timeout=150)
    ids = identities(ctx)
    jobs = owned_jobs(containers(), ids, backend.node)
    current = {c['Id']: c for c in containers()}
    for row in jobs:
        if not inactive(current[row['id']]):
            require(not current[row['id']]['State'].get('Paused'), 'Unpause the selected platform Job before preparation')
            docker('stop', '--time', '20', row['id'], timeout=60)
    cold(ctx, jobs)
    ids = identities(ctx)
    plan = {'format': 1, 'scope': 'local-platform-test-reset', 'created_at_unix': int(time.time()),
            'binding': backend.binding(), 'identities': ids, 'jobs': jobs, 'databases': db_inventory(ctx),
            'kafka': backend.kafka_inventory(), 'buckets': backend.objects(ids),
            'volumes': {k: backend.volume(k, ids) for k in KINDS}, 'mlflow_runs': backend.mlflow_runs(ids),
            'preserved': ['source/configuration/images', 'shared PostgreSQL/MinIO/Kafka services and other data',
                          'MLflow experiments and deleted-run tombstones', 'tests/results reports']}
    cold(ctx, jobs)
    require(db_inventory(ctx) == plan['databases'], 'Database changed during preparation')
    private_json(plan_path, plan)
    return plan


def load_plan(path):
    p = Path(path)
    require(p.is_file() and not p.is_symlink() and p.stat().st_size <= 64 * 1024 ** 2, 'Invalid reset plan file')
    plan = json.loads(p.read_text())
    require(plan.get('format') == 1 and plan.get('scope') == 'local-platform-test-reset', 'Unknown reset plan format')
    return plan


def apply(ctx, backend, plan, receipt_path):
    require(backend.binding() == plan['binding'], 'Configuration/container identity changed; prepare a new plan')
    ids, jobs = plan['identities'], plan['jobs']
    guard = lambda: cold(ctx, jobs)
    guard()
    subset_state(db_inventory(ctx), plan['databases'])
    current_jobs = owned_jobs(containers(), ids, backend.node)
    require(all(r in jobs for r in current_jobs), 'Runtime identity changed after preparation')
    # Read and validate every component before the first irreversible action.
    objects = backend.objects(ids)
    for k, value in objects.items():
        old = plan['buckets'][k]
        require(all(r in old['objects'] for r in value['objects']) and all(r in old['uploads'] for r in value['uploads']), 'S3 changed after preparation')
    for k in KINDS:
        entries = {r['path']: r for r in plan['volumes'][k]}
        require(all(entries.get(r['path']) == r for r in backend.volume(k, ids)), 'Volume changed after preparation')
    require(all(r in plan['mlflow_runs'] for r in backend.mlflow_runs(ids)), 'MLflow changed after preparation')
    require(backend.kafka_inventory() == plan['kafka'], 'Kafka changed after preparation')
    journal = Path(receipt_path)
    private_json(journal, {'passed': False, 'plan_sha256': hash_json(plan), 'phase': 'started', 'completed': []})
    receipt = json.loads(journal.read_text())
    def record(phase, result):
        receipt['phase'] = phase; receipt['completed'].append({'step': phase, 'result': result})
        temporary = journal.with_name(journal.name + '.tmp-' + uuid.uuid4().hex)
        private_json(temporary, receipt)
        os.replace(temporary, journal)
        print('[reset] ' + phase, flush=True)
    # Empty the old event log before erasing Operation identities.
    record('kafka', backend.clear_kafka(plan['kafka']))
    record('mlflow', backend.clear_mlflow(plan['mlflow_runs'], ids, guard))
    record('objects', backend.clear_objects(plan['buckets'], ids, guard))
    for row in current_jobs:
        guard()
        docker('rm', row['id'])  # Never force-remove a concurrently restarted runtime.
    record('runtimes', {'removed': [r['id'] for r in current_jobs]})
    for kind in KINDS:
        guard()
        record(kind, backend.volume(kind, ids, 'apply', plan['volumes'][kind]))
    for db in DATABASES:
        guard()
        subset_state(db_inventory(ctx), plan['databases'])
        selected = checked_tables(db, {('public', t) for t in plan['databases'][db]})
        names = ','.join('public."' + t + '"' for t in selected)
        ctx.sql(db, "BEGIN; SET LOCAL lock_timeout='5s'; TRUNCATE TABLE " + names + ' RESTART IDENTITY; COMMIT;')
        record(db, {'empty': True})
    guard()
    require(all(r['count'] == 0 for t in db_inventory(ctx).values() for r in t.values()), 'Database reset incomplete')
    require(all(not v['objects'] and not v['uploads'] for v in backend.objects(ids).values()), 'Objects remain')
    require(all(not backend.volume(k, ids) for k in KINDS), 'Volume reset incomplete')
    require(backend.offsets(-2) == plan['kafka']['end'] == backend.offsets(-1), 'Kafka changed during reset')
    receipt['passed'] = True
    record('complete', {'platform_services': 'stopped', 'data_backup': False, 'mlflow_mode': 'soft-delete'})
    return receipt


@contextmanager
def reset_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OpsError('Another reset command is already running') from None
        yield
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    p = subs.add_parser('prepare', help='Stops the platform and its owned runtimes; writes a plan without deleting data')
    p.add_argument('--plan', required=True)
    p = subs.add_parser('apply', help='Irreversibly resets only the prepared test resources; no backup')
    p.add_argument('--plan', required=True)
    p.add_argument('--report', required=True)
    args = parser.parse_args()
    target = Path(args.plan if args.command == 'prepare' else args.report)
    require(not target.exists() and not target.is_symlink(), 'Use a new output filename')
    ctx = Context(); backend = Backend(ctx)
    if args.command == 'prepare':
        plan = prepare(ctx, backend, args.plan)
        summary = {'plan': str(Path(args.plan).absolute()), 'services': 'stopped', 'data_deleted': False,
                   'tables': {db: {t: r['count'] for t, r in rows.items()} for db, rows in plan['databases'].items()},
                   'buckets': {plan['binding']['buckets'][k]: {'objects': len(v['objects']), 'bytes': sum(r['size'] for r in v['objects']), 'multipart_uploads': len(v['uploads'])} for k, v in plan['buckets'].items()},
                   'volumes': {k: {'entries': len(v), 'bytes': sum(r.get('size', 0) for r in v)} for k, v in plan['volumes'].items()},
                   'runtimes': len(plan['jobs']), 'mlflow_runs': len(plan['mlflow_runs']), 'preserved': plan['preserved']}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print('PREPARED: test data retained; apply this exact plan to reset.')
    else:
        apply(ctx, backend, load_plan(args.plan), args.report)
        print('[report] ' + str(Path(args.report).absolute()))
        print('PASS: prepared test resources reset; platform remains stopped for restart validation.')


if __name__ == '__main__':
    try:
        with reset_lock(ROOT / 'tests/results/operations/reset.lock'):
            main()
    except Exception as exc:
        print('FAIL: ' + (str(exc) if isinstance(exc, OpsError) else type(exc).__name__), file=sys.stderr)
        print('Do not restart the platform after a partial reset; keep the plan and receipt for the same-plan retry.', file=sys.stderr)
        sys.exit(1)
