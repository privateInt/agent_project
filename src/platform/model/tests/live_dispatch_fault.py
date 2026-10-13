"""Isolated CPU-only dispatch crash / real lease expiry / stale-token smoke.
Requires the launch-v1 rollout and the test-only dispatch-fault image.
Uses Python stdlib. Never stops the normal Compose services or changes their rows.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.parse
import uuid
from smoke_timestamps import timestamp_key

TEST_IMAGE = 'model-platform-dispatch-fault:20260908'
LABEL = 'model-platform.dispatch-smoke'
DATABASES = ('model_operations', 'model_deployment')
TOPICS_CLI = '/opt/kafka/bin/kafka-topics.sh'
GROUPS_CLI = '/opt/kafka/bin/kafka-consumer-groups.sh'


def require(value, message):
    if not value:
        raise RuntimeError(message)


def identifier(value):
    require(bool(re.fullmatch(r'dispatch_smoke_[0-9a-f]{32}', value)), 'invalid test schema')
    return value


def scoped_url(value, schema):
    identifier(schema)
    url = urllib.parse.urlsplit(value)
    require(url.scheme in ('postgres', 'postgresql') and url.username, 'POSTGRES_URL must be a PostgreSQL URL')
    query = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
    require('search_path' not in query.get('options', ''), 'remove conflicting search_path from PostgreSQL options before this test')
    query['search_path'] = schema  # No public fallback.
    return urllib.parse.urlunsplit(url._replace(query=urllib.parse.urlencode(query)))


def fence_snapshot(row):
    return {k: row[k] for k in ('operation_id', 'status', 'phase', 'attempt', 'dispatch_generation',
                              'dispatch_token', 'execution_instance_id', 'execution_started_at',
                              'completed_at', 'updated_at')}


def summarize(row):
    if not row:
        return row
    return {k: row.get(k) for k in ('operation_id', 'status', 'phase', 'attempt', 'dispatch_generation',
                                   'execution_started_at', 'reconcile_note')}


class Lab:
    def __init__(self, args):
        self.args = args
        self.run_id = str(uuid.uuid4())
        suffix = uuid.UUID(self.run_id).hex
        self.schema = identifier('dispatch_smoke_' + suffix)
        self.topic = 'ops.smoke.dispatch.' + suffix
        self.group = 'model-platform-smoke-' + suffix
        self.prefix = 'dispatch-smoke-' + suffix[:12]
        self.names = {role: self.prefix + '-' + role for role in ('operation', 'deployment', 'fault', 'recovery', 'reconciler', 'stale')}
        self.schemas = []
        self.topic_owned = False
        self.group_used = False
        self.secrets = []
        self.op_id = None
        self.report = {'run_id': self.run_id, 'topic': self.topic, 'group': self.group, 'schema': self.schema, 'passed': False}
        self.output = Path(args.output).resolve() / self.run_id
        self.output.mkdir(parents=True, exist_ok=False)
        self.temporary = tempfile.TemporaryDirectory(prefix=self.prefix + '-')
        self.temp = Path(self.temporary.name)
        self.env_count = 0

    def redact(self, value):
        for secret in sorted(set(self.secrets), key=len, reverse=True):
            if secret:
                value = value.replace(secret, '[redacted]')
        return value

    def command(self, args, stdin=None, timeout=25, check=True):
        result = subprocess.run(args, input=stdin, text=True, capture_output=True, timeout=timeout)
        if check and result.returncode:
            message = self.redact(result.stderr.strip() or result.stdout.strip())
            raise RuntimeError('command failed (' + ' '.join(args[:3]) + '): ' + message[-2500:])
        return result

    def docker(self, *args, **kwargs):
        return self.command(['docker', *args], **kwargs)

    def sql(self, database, statement):
        require(database in DATABASES, 'unexpected database')
        return self.docker('exec', '-i', self.args.postgres_container, 'sh', '-c',
                           'exec psql -X -qAt -U "$POSTGRES_USER" -d "$1" -v ON_ERROR_STOP=1',
                           'psql', database, stdin=statement).stdout.strip()

    def row(self, database, query):
        text = self.sql(database, 'SELECT row_to_json(t) FROM (' + query + ') t;')
        return json.loads(text) if text else None

    def operation(self):
        return self.row('model_operations', f"SELECT *,clock_timestamp() AS observed_at FROM {self.schema}.operations WHERE operation_id='{self.op_id}'")

    def outbox(self):
        return self.row('model_operations', f"SELECT event_id,publish_attempts,published_at IS NOT NULL AS published FROM {self.schema}.operation_outbox WHERE operation_id='{self.op_id}'")

    def env_file(self, values):
        self.env_count += 1
        path = self.temp / ('env-' + str(self.env_count))
        for key, value in values.items():
            require(re.fullmatch(r'[A-Z_][A-Z_0-9]*', key) and '\n' not in str(value) and '\r' not in str(value), 'invalid environment entry')
        path.write_text(''.join(k + '=' + str(v) + '\n' for k, v in values.items()))
        path.chmod(0o600)
        return str(path)

    def inspect(self, name):
        # Keep environment values out of diagnostic output.
        result = self.docker('inspect', '--type', 'container', '--format',
                             '{"Name":{{json .Name}},"State":{{json .State}},"Labels":{{json .Config.Labels}}}', name, check=False)
        if result.returncode:
            require('No such' in result.stderr, 'cannot inspect container ' + name)
            return None
        return json.loads(result.stdout)

    def start(self, role, binary, env, extra=(), image=TEST_IMAGE):
        name = self.names[role]
        require(self.inspect(name) is None, 'test container name already exists')
        self.docker('run', '-d', '--name', name, '--label', LABEL + '=' + self.run_id,
                    '--network', self.network, '--memory', '256m', '--cpus', '0.5',
                    '--env-file', self.env_file(env), *extra, image, binary)
        return name

    def helper(self, *arguments, env=None):
        name = self.prefix + '-helper-' + uuid.uuid4().hex[:8]
        result = self.docker('run', '--rm', '--name', name, '--label', LABEL + '=' + self.run_id,
                             '--network', self.network, '--memory', '192m', '--cpus', '0.5',
                             '--env-file', self.env_file(env or {}), TEST_IMAGE,
                             '/app/fault-driver', *arguments, timeout=45)
        return json.loads(result.stdout)

    def wait(self, label, getter, predicate, timeout, brief=lambda v: v):
        deadline = time.monotonic() + timeout
        last_print, printed = None, 0.0
        while time.monotonic() < deadline:
            value = getter()
            message = json.dumps(brief(value), sort_keys=True, ensure_ascii=False)
            if message != last_print or time.monotonic() - printed >= 15:
                print('[' + label + '] ' + message, flush=True)
                last_print, printed = message, time.monotonic()
            if predicate(value):
                return value
            if isinstance(value, dict) and value.get('status') in ('failed', 'cancelled'):
                raise RuntimeError(label + ' reached an unexpected terminal state')
            time.sleep(2)
        raise RuntimeError(label + ' timed out after ' + str(timeout) + ' seconds')

    def kafka(self, script, *args, **kwargs):
        return self.docker('exec', self.args.kafka_container, script, '--bootstrap-server', self.broker,
                           *args, timeout=25, **kwargs)

    def preflight(self):
        config = json.loads(self.docker('compose', 'config', '--format', 'json').stdout)
        values = config['services']['operation']['environment']
        dsn = values['POSTGRES_URL']
        self.secrets = [dsn, urllib.parse.unquote(urllib.parse.urlsplit(dsn).password or '')]
        self.dsn = scoped_url(dsn, self.schema)
        self.secrets.append(self.dsn)
        self.role = urllib.parse.unquote(urllib.parse.urlsplit(dsn).username)
        self.network = config['networks']['ops-infra']['name']
        self.brokers = values['KAFKA_BROKERS']
        self.broker = self.brokers.split(',')[0].strip()
        dispatch_env = config['services']['dispatcher']['environment']
        self.job_image = dispatch_env['DEPLOYMENT_JOB_IMAGE']
        self.management = dispatch_env.get('MANAGEMENT_GRPC_ADDRESS', 'management:9000')
        for name in (self.args.postgres_container, self.args.kafka_container):
            state = self.inspect(name)
            require(state and state['State']['Running'], name + ' must be running')
        self.docker('network', 'inspect', self.network)
        label = self.docker('image', 'inspect', '--format', '{{index .Config.Labels "model-platform.dispatch-fault-test"}}', TEST_IMAGE).stdout.strip()
        require(label == '20260908', 'build the test-only dispatch-fault image first')
        label = self.docker('image', 'inspect', '--format', '{{index .Config.Labels "model-platform.execution-gate"}}', self.job_image).stdout.strip()
        require(label == '1', 'deployment image must implement the execution gate')
        for script in (TOPICS_CLI, GROUPS_CLI):
            self.docker('exec', self.args.kafka_container, 'test', '-x', script)
        require(self.topic != values['KAFKA_COMMAND_TOPIC'], 'test topic overlaps normal topic')
        self.report['job_image'] = self.job_image
        print('[isolation] ' + json.dumps({'run_id': self.run_id, 'schema': self.schema, 'topic': self.topic}), flush=True)

    def setup(self):
        marker = 'dispatch-smoke:' + self.run_id
        quoted_role = '"' + self.role.replace('"', '""') + '"'
        for database in DATABASES:
            exists = self.sql(database, f"SELECT 1 FROM pg_namespace WHERE nspname='{self.schema}';")
            require(not exists, 'schema already exists; refusing reuse')
            self.schemas.append(database)  # Also covers a lost successful response.
            self.sql(database, f"BEGIN; CREATE SCHEMA {self.schema} AUTHORIZATION {quoted_role}; COMMENT ON SCHEMA {self.schema} IS '{marker}'; COMMIT;")
        self.helper('--mode=db-scope', '--schema=' + self.schema, env={'POSTGRES_URL': self.dsn})
        existing = self.kafka(TOPICS_CLI, '--list').stdout.splitlines()
        require(self.topic not in existing, 'topic already exists; refusing reuse')
        self.topic_owned = True
        self.kafka(TOPICS_CLI, '--create', '--topic', self.topic, '--partitions', '1', '--replication-factor', '1', '--config', 'min.insync.replicas=1')
        self.start('operation', '/app/operation', {'POSTGRES_URL': self.dsn, 'KAFKA_BROKERS': self.brokers,
                   'KAFKA_COMMAND_TOPIC': self.topic, 'GRPC_ADDRESS': ':9000'})
        self.operation_address = self.names['operation'] + ':9000'
        self.deployment_address = self.names['deployment'] + ':9000'
        self.helper('--mode=health', '--address=' + self.operation_address)
        self.start('deployment', '/app/deployment', {'POSTGRES_URL': self.dsn, 'OPERATION_GRPC_ADDRESS': self.operation_address,
                   'MANAGEMENT_GRPC_ADDRESS': self.management, 'GRPC_ADDRESS': ':9000'})
        self.helper('--mode=health', '--address=' + self.deployment_address)
        self.dispatch_env = {'KAFKA_BROKERS': self.brokers, 'KAFKA_COMMAND_TOPIC': self.topic,
                             'KAFKA_CONSUMER_GROUP': self.group, 'JOB_NETWORK': self.network,
                             'OPERATION_GRPC_ADDRESS': self.operation_address,
                             'DEPLOYMENT_GRPC_ADDRESS': self.deployment_address,
                             'DEPLOYMENT_JOB_IMAGE': self.job_image}

    def execute(self):
        print('[1/4] Create isolated CPU deployment, then pause the real Dispatcher before docker run', flush=True)
        result = self.helper('--mode=deploy', '--address=' + self.deployment_address,
                             '--model-version-id=' + self.args.model_version_id)
        self.op_id = str(uuid.UUID(result['operation_id']))
        self.dep_id = str(uuid.UUID(result['deployment_id']))
        self.report.update(operation_id=self.op_id, deployment_id=self.dep_id)
        event = self.wait('first-publication', self.outbox, lambda v: v and v['published'], 60)
        self.event_id = event['event_id']
        env = dict(self.dispatch_env, PATH='/fault-bin:/usr/local/bin:/usr/bin:/bin', FAULT_RUN_ID=self.run_id)
        self.group_used = True
        self.start('fault', '/app/dispatcher', env, ('-v', '/var/run/docker.sock:/var/run/docker.sock',
                   '--mount', 'type=bind,src=' + str(self.temp) + ',dst=/fault-evidence'))
        claim_path = self.temp / 'claim.json'
        # Poll locally, faster than the dispatcher's 30-second launch timeout.
        deadline = time.monotonic() + 60
        while not claim_path.exists():
            state = self.inspect(self.names['fault'])
            require(state and state['State']['Running'], 'fault dispatcher exited before the injection point')
            require(time.monotonic() < deadline, 'dispatcher did not reach docker launch')
            time.sleep(0.2)
        evidence = json.loads(claim_path.read_text())
        require(evidence['operation_id'] == self.op_id and evidence['resource_id'] == self.dep_id, 'fault intercepted an unexpected operation')
        first = self.operation()
        require(first['status'] == 'dispatching' and first['dispatch_generation'] == 1 and first['execution_started_at'] is None, 'first claim already executed or has the wrong generation')
        self.old_token = str(uuid.UUID(evidence['dispatch_token']))
        require(first['dispatch_token'] == self.old_token, 'claim evidence does not match DB ownership')
        self.docker('kill', '--signal', 'KILL', self.names['fault'])
        state = self.wait('dispatcher-exit', lambda: self.inspect(self.names['fault']),
                          lambda v: v and not v['State']['Running'], 30, brief=lambda v: v and v['State']['Status'])
        require(state['State']['ExitCode'] == 137 and not state['State']['OOMKilled'], 'dispatcher was not stopped by the intended SIGKILL')
        old_result = self.docker('logs', self.names['fault'])
        old_logs = old_result.stdout + old_result.stderr
        require('record handled' not in old_logs, 'fault dispatcher committed before the crash')
        self.report['crash'] = {'exit_code': 137, 'generation': 1, 'execution_started': False}
        print('[crash] ' + json.dumps(self.report['crash']), flush=True)

        print('[2/4] Wait for the real 120-second lease and Reconciler/Outbox recovery', flush=True)
        self.start('reconciler', '/app/reconciler', {'OPERATION_GRPC_ADDRESS': self.operation_address,
                   'DEPLOYMENT_GRPC_ADDRESS': self.deployment_address, 'MANAGEMENT_GRPC_ADDRESS': self.management},
                   ('-v', '/var/run/docker.sock:/var/run/docker.sock'))
        pending = self.wait('lease-recovery', self.operation,
                            lambda v: v['status'] == 'pending' and v['phase'] == 'launch_retry_queued',
                            self.args.timeout, brief=summarize)
        require(pending['dispatch_token'] is None and pending['execution_started_at'] is None and pending['dispatch_generation'] == 1, 'requeue retained execution ownership')
        require(timestamp_key(pending['observed_at']) >= timestamp_key(first['dispatch_lease_until']), 'recovery occurred before the original lease expired')
        second_event = self.wait('republication', self.outbox,
                                 lambda v: v and v['published'] and v['publish_attempts'] >= event['publish_attempts'] + 1, 60)
        require(second_event['event_id'] == self.event_id, 'recovery changed event identity')

        print('[3/4] Start an unmodified recovery Dispatcher with the same consumer group', flush=True)
        self.start('recovery', '/app/dispatcher', self.dispatch_env,
                   ('-v', '/var/run/docker.sock:/var/run/docker.sock'))
        done = self.wait('recovered-job', self.operation, lambda v: v['status'] == 'succeeded',
                         self.args.timeout, brief=summarize)
        require(done['dispatch_generation'] == 2 and done['attempt'] == 1 and done['dispatch_token'] != self.old_token,
                'expected one new launch generation without a new computation attempt')
        require(done['execution_started_at'] and done['execution_instance_id'], 'recovered job did not receive an execution grant')
        domain = self.row('model_deployment', f"SELECT * FROM {self.schema}.deployments WHERE deployment_id='{self.dep_id}'")
        require(domain and domain['status'] == 'ready', 'recovered demo deployment is not ready')
        job_name = 'model-op-' + self.op_id + '-' + done['dispatch_token']
        self.wait('job-exit', lambda: self.inspect(job_name),
                  lambda v: v and not v['State']['Running'], 30, brief=lambda v: v and v['State']['Status'])
        def handled_offsets():
            r = self.docker('logs', self.names['recovery'])
            return sorted(set(int(n) for n in re.findall('record handled topic=' + re.escape(self.topic) + r' partition=0 offset=(\d+)', r.stdout + r.stderr)))
        offsets = self.wait('committed-delivery', handled_offsets, lambda v: 0 in v and 1 in v, 30)
        self.report['handled_offsets'] = offsets
        self.report['recovery'] = {'dispatch_generation': 2, 'attempt': 1, 'event_id': self.event_id,
                                   'publish_attempts': second_event['publish_attempts'], 'status': done['status']}

        print('[4/4] Launch a delayed old-token Job; it must stop before domain work', flush=True)
        old_env = {'OPERATION_ID': self.op_id, 'RESOURCE_ID': self.dep_id, 'RESOURCE_TYPE': 'deployment',
                   'EXECUTION_TYPE': 'deployment.apply', 'DISPATCH_TOKEN': self.old_token,
                   'OPERATION_GRPC_ADDRESS': self.operation_address, 'DEPLOYMENT_GRPC_ADDRESS': self.deployment_address}
        self.start('stale', '/app/deployment-apply', old_env, image=self.job_image)
        self.wait('stale-job-exit', lambda: self.inspect(self.names['stale']),
                  lambda v: v and not v['State']['Running'], 30, brief=lambda v: v and v['State']['Status'])
        logs = self.docker('logs', self.names['stale'])
        text = logs.stdout + logs.stderr
        require('stale execution token or input identity mismatch' in text and 'execution not authorized; process stopped' in text,
                'old Job did not explicitly reject the stale token')
        require('demo deployment completed' not in text, 'stale Job performed domain completion')
        require(fence_snapshot(self.operation()) == fence_snapshot(done), 'stale Job changed the winning operation')
        after = self.row('model_deployment', f"SELECT * FROM {self.schema}.deployments WHERE deployment_id='{self.dep_id}'")
        require(after == domain, 'stale Job modified the deployment domain row')
        self.report['stale_token_rejected'] = True
        print('[verified] ' + json.dumps(self.report['recovery'], sort_keys=True), flush=True)

    def remove_owned(self, name):
        value = self.inspect(name)
        if not value:
            return
        require((value['Labels'] or {}).get(LABEL) == self.run_id, 'refusing to remove foreign container ' + name)
        self.docker('rm', '-f', name)

    def collect_logs(self):
        for role, name in self.names.items():
            result = self.docker('logs', '--tail', '100', name, check=False)
            if result.returncode == 0:
                (self.output / (role + '.log')).write_text(self.redact(result.stdout + result.stderr))

    def cleanup(self):
        errors = []
        def attempt(label, action):
            try:
                action()
            except Exception as exc:
                errors.append(label + ': ' + self.redact(str(exc)))
        # Stop all sources of new launch requests before finding owned jobs.
        for role in ('fault', 'recovery', 'reconciler'):
            attempt(role, lambda r=role: self.remove_owned(self.names[r]))
        operation_ids = []
        if 'model_operations' in self.schemas:
            try:
                exists = self.sql('model_operations', f"SELECT to_regclass('{self.schema}.operations') IS NOT NULL;")
                if exists == 't':
                    operation_ids = self.sql('model_operations', f"SELECT operation_id FROM {self.schema}.operations;").splitlines()
                    self.sql('model_operations', f"UPDATE {self.schema}.operations SET status='cancelled',phase='fault_smoke_cleanup',updated_at=NOW(),completed_at=NOW() WHERE status IN ('pending','dispatching','running','retry_wait');")
            except Exception as exc:
                errors.append('fence fixtures: ' + self.redact(str(exc)))
        for operation_id in operation_ids:
            try:
                operation_id = str(uuid.UUID(operation_id))
                jobs = self.docker('ps', '-aq', '--filter', 'label=model-platform.owner=operation-dispatcher',
                                   '--filter', 'label=model-platform.operation_id=' + operation_id).stdout.splitlines()
                for job in jobs:
                    value = self.inspect(job)
                    labels = value['Labels'] or {}
                    token = str(uuid.UUID(labels.get('model-platform.dispatch_token', '')))
                    require(value['Name'] == '/model-op-' + operation_id + '-' + token and labels.get('model-platform.execution_type') == 'deployment.apply',
                            'refusing unexpected job identity during cleanup')
                    logs = self.docker('logs', '--tail', '30', job, check=False)
                    (self.output / ('job-' + operation_id + '.log')).write_text(self.redact(logs.stdout + logs.stderr))
                    self.docker('rm', '-f', job)
            except Exception as exc:
                errors.append('job cleanup: ' + self.redact(str(exc)))
        for role in ('stale', 'deployment', 'operation'):
            attempt(role, lambda r=role: self.remove_owned(self.names[r]))
        # Catch a helper container left behind if a Docker client timed out.
        try:
            for name in self.docker('ps', '-aq', '--filter', 'label=' + LABEL + '=' + self.run_id).stdout.splitlines():
                self.remove_owned(name)
        except Exception as exc:
            errors.append('helper cleanup: ' + self.redact(str(exc)))
        for database in self.schemas:
            def drop(db=database):
                marker = self.sql(db, f"SELECT obj_description(oid,'pg_namespace') FROM pg_namespace WHERE nspname='{self.schema}';")
                if not marker:
                    return
                require(marker == 'dispatch-smoke:' + self.run_id, 'refusing schema with a different ownership marker')
                self.sql(db, f'DROP SCHEMA {self.schema} CASCADE;')
            attempt(database, drop)
        if self.topic_owned:
            def delete_topic():
                self.kafka(TOPICS_CLI, '--delete', '--if-exists', '--topic', '^' + re.escape(self.topic) + '$')
                deadline = time.monotonic() + 30
                while self.topic in self.kafka(TOPICS_CLI, '--list').stdout.splitlines():
                    require(time.monotonic() < deadline, 'test topic deletion has not completed')
                    time.sleep(2)
            attempt('topic', delete_topic)
        if self.group_used:
            def delete_group():
                # SIGKILL does not send LeaveGroup; let the broker expire the
                # isolated member. Only this invocation's exact group is used.
                deadline = time.monotonic() + 75
                while True:
                    result = self.kafka(GROUPS_CLI, '--delete', '--group', self.group, check=False)
                    message = result.stdout + result.stderr
                    remaining = self.kafka(GROUPS_CLI, '--list', check=False)
                    if remaining.returncode == 0 and self.group not in remaining.stdout.splitlines():
                        return
                    require(time.monotonic() < deadline, 'could not remove test consumer group: ' + message[-1000:])
                    time.sleep(3)
            attempt('consumer group', delete_group)
        attempt('temporary files', self.temporary.cleanup)
        return errors

    def run(self):
        failure = None
        try:
            self.preflight()
            self.setup()
            self.execute()
            self.report['passed'] = True
        except (Exception, KeyboardInterrupt) as exc:
            failure = self.redact(str(exc) or type(exc).__name__)
            self.report['error'] = failure
        finally:
            try:
                self.collect_logs()
            except Exception as exc:
                self.report['log_collection_error'] = self.redact(str(exc))
            print('[cleanup] Removing only this test run\'s resources', flush=True)
            cleanup_errors = self.cleanup()
            self.report['cleanup_errors'] = cleanup_errors
            self.report['passed'] = self.report['passed'] and not cleanup_errors
            report_path = self.output / 'report.json'
            report_path.write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + '\n')
            print('[report] ' + str(report_path), flush=True)
        if failure or cleanup_errors:
            raise RuntimeError('; '.join(([failure] if failure else []) + cleanup_errors))
        print('PASS: real Dispatcher SIGKILL, natural lease expiry, Outbox republication, generation-2 completion, stale-token rejection; isolated resources cleaned.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-version-id', default='cceae526-7b7c-47d4-870b-117372b51885')
    parser.add_argument('--postgres-container', default='postgres')
    parser.add_argument('--kafka-container', default='ops-kafka')
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--output', default='tests/results/dispatch-fault')
    args = parser.parse_args()
    args.model_version_id = str(uuid.UUID(args.model_version_id))
    require(180 <= args.timeout <= 600, '--timeout must be 180..600 seconds')
    require(Path('docker-compose.yml').is_file(), 'run from ~/workspace/agent_project/model_platform')
    Lab(args).run()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('FAIL: ' + str(exc), flush=True)
        raise SystemExit(1)
