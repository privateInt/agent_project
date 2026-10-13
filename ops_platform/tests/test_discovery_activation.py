"""Initial discovery activation: provenance, interruption/rollback and real Ops HTTP."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import fastapi
import uvicorn
from ops_platform.alerts import GROUP, RULES, SCOPE
from ops_platform.contracts import ReadFailure
from ops_platform.ops import activate_discovery as activation, activation_plan, manage, restart_check as rc
from ops_platform.ops import set_public_url as url_change
from ops_platform.resource_profiles import DEFAULT_PROFILE
from ops_platform.tests.test_restart_check import Fixture, IDENT, TOKEN, LF_SECRET
from ops_platform.tests.test_discovery import Fixture as HTTPFixture


def proposal(cfg):
    profile = deepcopy(DEFAULT_PROFILE)
    profile['sources']['host']['job'] = 'discovered-machine'
    profile['target_jobs'] = ['discovered-machine', 'containers', 'gpu', 'prometheus']
    profile['filesystems']['mountpoint_include'] = '/|/srv/discovered'
    endpoints = {role: cfg['platforms'][role]['url'] for role in activation_plan.PLATFORMS}
    endpoints.update({role: cfg.get(role + '_url', 'http://127.0.0.1:24321') for role in activation_plan.OBSERVATIONS})
    return {'result': 'discovered', 'deployment_metadata_stable': True,
            'connection_overlay': {'platforms': {role: {'url': endpoints[role]} for role in activation_plan.PLATFORMS},
                                   **{role + '_url': endpoints[role] for role in activation_plan.OBSERVATIONS}},
            'metrics': {'generated_profile': profile},
            'services': {role: {'identity': {'provider': 'docker-compose', 'project': 'fixture', 'service': role}}
                         for role in endpoints}}


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture(); self.addCleanup(self.f.close)
        self.docker = self.f.docker
        self.protected = {'prometheus': 'original-prometheus', 'agent': 'original-agent'}
        self.docker.protected = lambda ident: deepcopy(self.protected)
        self.mounted = rc.read_file(self.f.directory / 'config.json')[1]['sha256']
        self.docker.mounted_hash = lambda ident: self.mounted
        self.failure = None
        old_restart = self.docker.restart
        def restart(ident):
            record = activation.load_record(self.evidence, self.f.directory)
            intent = record['rollback']['restart_intent'] if record.get('rollback') else record['restart_intent']
            self.assertEqual(intent['id'], IDENT)
            if self.failure == 'before':
                self.docker.restarts += 1
                raise ReadFailure('DOCKER_TIMEOUT')
            old_restart(ident)
            self.mounted = rc.read_file(self.f.directory / 'config.json')[1]['sha256']
            if self.failure == 'after': raise ReadFailure('DOCKER_TIMEOUT')
        self.docker.restart = restart
        for rel in ('restart-check/baseline.json', 'public-url-change/baseline.json'):
            path = self.f.directory / rel; path.parent.mkdir(mode=0o700)
            manage.write_new(path, {'keep': rel})
        self.report = proposal(self.f.cfg)
        self.source = 'original-deployment-binding'
        self.validated = []
        self.reads = []
        def validate(cfg): self.validated.append(deepcopy(cfg)); return {'synthetic': True}
        def readback(cfg): self.reads.append(deepcopy(cfg)); return {'synthetic': True}
        self.validate = validate; self.readback = readback

    @property
    def evidence(self): return self.f.directory / 'discovery-activation/baseline.json'

    def call(self, action='apply', **kwargs):
        options = dict(docker=self.docker, discover_fn=lambda cfg: deepcopy(self.report),
                       validate_fn=self.validate, source_fn=lambda: self.source,
                       api_factory=lambda cfg: self.f.api, readback_fn=self.readback, wait_seconds=0)
        options.update(kwargs)
        return activation.run(action, self.f.directory, **options)

    def error(self, code, action='apply', **kwargs):
        with self.assertRaises(ReadFailure) as caught: self.call(action, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def config_snapshot(self):
        return {name: (self.f.directory / name).read_bytes() for name in activation.CONFIGS}

    def replace_configs(self, update):
        for name in activation.CONFIGS:
            path = self.f.directory / name; cfg = json.loads(path.read_text()); update(cfg)
            rc.write_record(path, cfg)
        self.f.cfg = manage.existing(self.f.directory)
        self.mounted = rc.read_file(self.f.directory / 'config.json')[1]['sha256']

    def test_success_preserves_overrides_keys_cache_history_and_records_provenance(self):
        before = rc.files(self.f.directory, self.f.cfg)
        configs = self.config_snapshot(); cache = self.f.cache.read_bytes(); history = activation.history(self.f.directory)
        result = self.call()
        self.assertEqual(result['result'], 'passed')
        self.assertEqual(result['changed_fields'], ['alertmanager_url', 'resource_profile'])
        cfg = manage.existing(self.f.directory)
        for key, value in self.f.cfg.items(): self.assertEqual(cfg[key], value)
        self.assertEqual(cfg['resource_profile']['sources']['host']['job'], 'discovered-machine')
        self.assertEqual(self.f.cache.read_bytes(), cache)
        self.assertEqual(activation.history(self.f.directory), history)
        after = rc.files(self.f.directory, cfg)
        for name in set(before) - set(activation.CONFIGS): self.assertEqual(after[name], before[name])
        record = activation.load_record(self.evidence, self.f.directory)
        self.assertEqual(record['baseline']['original'], {k: v.decode() for k, v in configs.items()})
        fields = record['baseline']['plan']['fields']
        self.assertEqual(fields['prometheus_url']['source'], 'existing_preserved')
        self.assertFalse(fields['prometheus_url']['automatic_future_update_allowed'])
        self.assertEqual(fields['resource_profile']['source'], 'discovery')
        raw = self.evidence.read_text()
        for secret in (TOKEN, LF_SECRET, (self.f.directory / 'access-key').read_text().strip()): self.assertNotIn(secret, raw)
        self.assertEqual(self.evidence.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.evidence.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(self.f.api.sessions)
        for action in ('apply', 'check'):
            repeated = self.call(action)
            self.assertTrue(repeated['evidence_reused']); self.assertFalse(repeated['live_checks_performed'])
        self.assertEqual(self.docker.restarts, 1); self.assertEqual(len(self.reads), 1)

    def test_plan_does_not_write_or_restart(self):
        before = self.config_snapshot()
        result = self.call('plan')
        self.assertEqual(result['result'], 'planned')
        self.assertFalse(result['configuration_written'])
        self.assertEqual(self.config_snapshot(), before)
        self.assertFalse(self.evidence.parent.exists()); self.assertEqual(self.docker.restarts, 0)

    def test_existing_different_urls_and_profile_are_preserved_and_validated(self):
        def override(cfg):
            cfg['resource_profile'] = deepcopy(DEFAULT_PROFILE)
            cfg['prometheus_url'] = 'http://127.0.0.1:29876'
            cfg['alertmanager_url'] = 'http://127.0.0.1:29877'
        self.replace_configs(override)
        before = self.config_snapshot()
        result = self.call()
        self.assertEqual(result['changed_fields'], [])
        self.assertEqual(self.docker.restarts, 0); self.assertEqual(self.config_snapshot(), before)
        self.assertTrue(all(row['prometheus_url'].endswith(':29876') for row in self.validated))
        fields = activation.load_record(self.evidence, self.f.directory)['baseline']['plan']['fields']
        self.assertFalse(fields['prometheus_url']['matches_discovery'])
        self.assertEqual(fields['resource_profile']['source'], 'existing_preserved')

    def test_prometheus_override_cannot_receive_profile_from_different_source(self):
        self.replace_configs(lambda cfg: cfg.update(prometheus_url='http://127.0.0.1:29876'))
        before = self.config_snapshot()
        self.error('PROFILE_SOURCE_CONFLICT')
        self.assertEqual(self.config_snapshot(), before); self.assertFalse(self.evidence.exists())

    def test_failed_discovery_does_not_activate_partial_proposal(self):
        self.report['result'] = 'attention_required'
        self.error('VERIFIED_DISCOVERY_REQUIRED')
        self.assertEqual(self.docker.restarts, 0); self.assertFalse(self.evidence.exists())

    def test_partial_pair_write_resumes_after_journal_without_second_baseline(self):
        original_write = activation.atomic_config
        def fail_second(path, *args):
            if path.name == 'config.json': raise OSError('synthetic disk failure')
            return original_write(path, *args)
        with patch.object(activation, 'atomic_config', side_effect=fail_second):
            with self.assertRaises(OSError): self.call()
        baseline = self.evidence.read_bytes()
        self.assertEqual(self.docker.restarts, 0)
        self.error('ACTIVATION_CONFIG_OR_CREDENTIAL_DRIFT', 'check')
        self.assertEqual(self.call()['result'], 'passed')
        self.assertEqual(json.loads(baseline)['baseline_sha256'], json.loads(self.evidence.read_bytes())['baseline_sha256'])
        self.assertEqual(self.docker.restarts, 1)

    def test_journal_write_failure_precedes_all_configuration_changes(self):
        before = self.config_snapshot()
        with patch.object(rc, 'write_record', side_effect=OSError('synthetic fsync error')):
            with self.assertRaises(OSError): self.call()
        self.assertEqual(self.config_snapshot(), before); self.assertEqual(self.docker.restarts, 0)

    def test_lost_restart_response_after_effect_check_does_not_restart(self):
        self.failure = 'after'; self.error('DOCKER_TIMEOUT')
        result = self.call('check')
        self.assertEqual(result['result'], 'passed')
        self.assertFalse(result['restart_command_completion_recorded'])
        self.assertEqual(self.docker.restarts, 1)

    def test_uncertain_restart_before_effect_is_not_replayed_or_rolled_back_blindly(self):
        self.failure = 'before'; self.error('DOCKER_TIMEOUT')
        self.error('OPS_ACTIVATION_NOT_READY')
        self.error('RESTART_EFFECT_UNKNOWN_CHECK_FIRST', 'rollback')
        self.assertEqual(self.docker.restarts, 1)

    def test_binding_change_before_write_refused(self):
        calls = 0
        def source():
            nonlocal calls
            calls += 1
            return 'original' if calls <= 2 else 'changed'
        before = self.config_snapshot()
        self.error('DISCOVERY_BINDING_CHANGED', source_fn=source)
        self.assertEqual(self.config_snapshot(), before); self.assertEqual(self.docker.restarts, 0)

    def test_binding_change_after_restart_fails_acceptance_and_can_rollback(self):
        before = self.config_snapshot()
        self.docker.after_restart = lambda: setattr(self, 'source', 'changed')
        self.error('DISCOVERY_BINDING_CHANGED')
        self.assertEqual(self.call('rollback')['result'], 'rolled_back')
        self.assertEqual(self.config_snapshot(), before); self.assertEqual(self.docker.restarts, 2)

    def test_rollback_after_partial_write_restores_exact_bytes_without_restart(self):
        # Deliberately noncanonical whitespace must survive rollback exactly.
        for name, raw in self.config_snapshot().items(): (self.f.directory / name).write_bytes(raw + b'\n ')
        self.mounted = rc.read_file(self.f.directory / 'config.json')[1]['sha256']
        before = self.config_snapshot(); write = activation.atomic_config
        def fail_second(path, *args):
            if path.name == 'config.json': raise OSError('fixture')
            return write(path, *args)
        with patch.object(activation, 'atomic_config', side_effect=fail_second):
            with self.assertRaises(OSError): self.call()
        self.assertEqual(self.call('rollback')['result'], 'rolled_back')
        self.assertEqual(self.config_snapshot(), before); self.assertEqual(self.docker.restarts, 0)
        self.assertTrue(self.call('rollback')['evidence_reused'])

    def test_rollback_after_activation_restarts_once_then_reuses(self):
        before = self.config_snapshot(); self.call()
        self.assertEqual(self.call('rollback')['result'], 'rolled_back')
        self.assertEqual(self.config_snapshot(), before); self.assertEqual(self.docker.restarts, 2)
        self.assertTrue(self.call('rollback')['evidence_reused']); self.assertEqual(self.docker.restarts, 2)
        self.docker.raw['started'] = '2027-01-01T00:00:00Z'
        self.error('ROLLED_BACK_OPS_CONTAINER_CHANGED', 'rollback')

    def test_rollback_restart_response_loss_resumes_without_duplicate(self):
        self.call(); self.failure = 'after'; self.error('DOCKER_TIMEOUT', 'rollback')
        self.failure = None
        self.assertEqual(self.call('rollback')['result'], 'rolled_back'); self.assertEqual(self.docker.restarts, 2)

    def test_credentials_and_previous_evidence_drift_stop_activation(self):
        self.failure = 'after'; self.error('DOCKER_TIMEOUT')
        key = self.f.directory / 'access-key'; before = key.read_bytes(); key.write_text('synthetic-change')
        self.error('ACTIVATION_CONFIG_OR_CREDENTIAL_DRIFT', 'check'); key.write_bytes(before)
        history = self.f.directory / 'public-url-change/baseline.json'; history.write_text('{}')
        self.error('PRIOR_ACCEPTANCE_RECORD_CHANGED', 'check'); self.assertEqual(self.docker.restarts, 1)

    def test_cache_loss_is_not_success(self):
        before = self.f.cache.read_bytes(); self.call()
        cache = json.loads(before); cache['success_times'].pop('1'*64); self.f.cache.write_text(json.dumps(cache))
        self.error('CACHE_ENTRY_LOST_OR_TIME_REVERSED', 'check'); self.f.cache.write_bytes(before)
        self.call('rollback'); self.assertEqual(self.docker.restarts, 2)

    def test_changed_protected_container_after_restart_prevents_acceptance(self):
        self.docker.after_restart = lambda: self.protected.update(agent='restarted')
        self.error('OTHER_CONTAINER_CHANGED'); self.assertEqual(self.docker.restarts, 1)

    def test_wrong_owner_mounted_config_and_history_race_fail_before_writes(self):
        self.docker.raw['owner'] = 'foreign'; self.error('CONTAINER_OWNER_MISMATCH')
        self.docker.raw['owner'] = self.f.owner['owner']
        self.mounted = 'wrong'; self.error('MOUNTED_CONFIG_DIFFERS')
        self.mounted = rc.read_file(self.f.directory / 'config.json')[1]['sha256']
        def discover(cfg):
            (self.f.directory / 'restart-check/baseline.json').write_text('{}')
            return self.report
        self.error('PRIOR_ACCEPTANCE_RECORD_CHANGED', discover_fn=discover)
        self.assertEqual(self.docker.restarts, 0); self.assertFalse(self.evidence.exists())

    def test_lock_symlink_tampered_record_and_check_without_record_refused(self):
        self.error('NO_ACTIVATION_RECORD', 'check')
        self.evidence.parent.symlink_to(self.f.directory / 'restart-check')
        self.error('SYMLINK_REFUSED'); self.evidence.parent.unlink()
        with url_change.change_lock(self.f.directory): self.error('PUBLIC_URL_CHANGE_BUSY')
        self.call(); record = json.loads(self.evidence.read_text()); record['baseline']['plan']['changed_fields'] = []
        rc.write_record(self.evidence, record); self.error('ACTIVATION_RECORD_INVALID', 'check')
        self.assertEqual(self.docker.restarts, 1)

    def test_api_readback_failure_keeps_original_journal_and_logs_out(self):
        def fail(cfg): raise ReadFailure('OPS_ALERT_READBACK_FAILED')
        self.error('OPS_ALERT_READBACK_FAILED', readback_fn=fail)
        self.assertFalse(self.f.api.sessions)
        self.assertNotIn('report', json.loads(self.evidence.read_text()))
        self.assertEqual(self.call('check')['result'], 'passed'); self.assertEqual(self.docker.restarts, 1)

    def test_fixed_docker_command_boundary_pins_daemon_and_rejects_other_actions(self):
        docker = object.__new__(activation.Docker)
        docker.endpoint = 'unix:///synthetic/docker.sock'; docker.env = {'PATH': '/synthetic'}
        with patch.object(activation.subprocess, 'run') as call:
            call.return_value.returncode = 0; call.return_value.stdout = IDENT + '\n'
            docker.restart(IDENT)
            args, kwargs = call.call_args
            self.assertEqual(args[0], ['docker', '--host', docker.endpoint, 'container', 'restart', '-t', '20', IDENT])
            self.assertEqual(kwargs['env'], docker.env)
            for forbidden in (['compose', 'up'], ['container', 'restart', '-t', '20', 'ops-web'],
                              ['exec', IDENT, 'sh', '-c', 'anything'], ['system', 'prune'], ['inspect', IDENT]):
                with self.assertRaises(ReadFailure): docker.command(forbidden)
            self.assertEqual(call.call_count, 1)

    def test_effective_validation_failure_happens_before_writes(self):
        before = self.config_snapshot()
        def fail(cfg): raise ReadFailure('EFFECTIVE_PROFILE_NOT_READY')
        self.error('EFFECTIVE_PROFILE_NOT_READY', validate_fn=fail)
        self.assertFalse(self.evidence.exists()); self.assertEqual(self.config_snapshot(), before)
        self.assertEqual(self.docker.restarts, 0)

    def test_effective_profile_rejects_down_target_empty_partial_or_unknown_values(self):
        cfg, _ = activation_plan.merge(self.f.cfg, self.report)
        good = {'status': 'ok', 'data': {'count': 1, 'coverage': 'query_result', 'items': [{'value': 1}]}}
        cases = [dict(good, status='partial'), {'status': 'ok', 'data': {'count': 0}},
                 {'status': 'ok', 'data': {'count': 1, 'coverage': 'query_result', 'items': [{'value': None}]}},
                 {'status': 'ok', 'data': {'count': 1, 'coverage': 'query_result', 'items': [{'value': 0}]}}]
        with patch.object(activation_plan, 'health'), patch.object(activation_plan, 'Prometheus') as prometheus:
            prometheus.return_value.queries = {'targets': None}
            for row in cases:
                prometheus.return_value.metric.return_value = row
                with self.assertRaises(ReadFailure) as caught: activation_plan.validate_effective(cfg)
                self.assertEqual(caught.exception.code, 'EFFECTIVE_PROFILE_NOT_READY')

    def test_runtime_readback_rejects_down_targets_and_missing_alert_sources(self):
        keys = ('targets', 'host_cpu', 'host_memory', 'filesystems', 'container_cpu', 'container_memory', 'gpu')
        good = {'status': 'ok', 'data': {'count': 1, 'coverage': 'query_result', 'items': [{'value': 1}]}}
        body = {'metrics': {key: deepcopy(good) for key in keys}}
        with patch.object(activation, 'Transport') as transport:
            body['metrics']['targets']['data']['items'][0]['value'] = 0
            transport.return_value.get.return_value = (200, body)
            with self.assertRaises(ReadFailure) as caught: activation.readback(self.f.cfg)
            self.assertEqual(caught.exception.code, 'OPS_RESOURCE_READBACK_FAILED')
            body['metrics']['targets'] = deepcopy(good)
            transport.return_value.get.side_effect = [(200, body), (200, {'status': 'ok', 'sources': {}})]
            with self.assertRaises(ReadFailure) as caught: activation.readback(self.f.cfg)
            self.assertEqual(caught.exception.code, 'OPS_ALERT_READBACK_FAILED')

    def test_real_web_restart_activates_queries_alert_source_and_preserves_sessions_contract(self):
        fixture = HTTPFixture(); self.addCleanup(fixture.close)
        original_response = fixture.response
        def response(path):
            if path.startswith('/api/v1/rules?'):
                rules = [{'name': name, 'type': 'alerting', 'duration': duration, 'labels': {'scope': SCOPE},
                          'state': 'inactive', 'health': 'ok', 'lastEvaluation': rc.now(), 'alerts': []}
                         for name, (_, duration) in RULES.items()]
                return {'status': 'success', 'data': {'groups': [{'name': GROUP, 'rules': rules}]}}
            if path.startswith('/api/v2/alerts?'): return []
            if path.startswith('/api/v1/query?'):
                query = parse_qs(urlsplit(path).query)['query'][0]
                body = original_response(path)
                if query.startswith('up{'):
                    body['data']['result'] = [fixture.sample({'job': 'discovered-machine', 'instance': 'fixture:1'})]
                return body
            return original_response(path)
        fixture.response = response
        def configure(cfg):
            cfg['prometheus_url'] = fixture.url; cfg['langfuse_url'] = fixture.url
            for role in cfg['platforms']: cfg['platforms'][role]['url'] = fixture.url
        self.replace_configs(configure); self.report = proposal(self.f.cfg)
        self.report['connection_overlay']['alertmanager_url'] = fixture.url
        root = Path(__file__).resolve().parents[2]
        env = dict(os.environ, OPS_CONFIG=str(self.f.directory / 'host-config.json'),
                   PYTHONPATH=os.pathsep.join(sorted({str(Path(m.__file__).resolve().parents[1]) for m in (fastapi, uvicorn)})))
        process = None
        with (self.f.root / 'process.log').open('w+') as log:
            def stop():
                nonlocal process
                if process is not None and process.poll() is None:
                    process.terminate()
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
            def start():
                nonlocal process
                process = subprocess.Popen([sys.executable, '-B', '-m', 'ops_platform.server'], cwd=root,
                                           env=env, stdout=log, stderr=log)
                deadline = time.monotonic() + 10
                while process.poll() is None and time.monotonic() < deadline:
                    try: rc.API(manage.existing(self.f.directory)).health(); return
                    except ReadFailure: time.sleep(.03)
                log.flush(); log.seek(0); self.fail(log.read()[-2000:])
            def restart(ident):
                self.assertEqual(ident, IDENT); self.docker.restarts += 1
                stop(); start(); self.docker.raw['started'] = rc.now()
                self.mounted = rc.read_file(self.f.directory / 'config.json')[1]['sha256']
            try:
                start(); old_cookie = rc.API(self.f.cfg).login()
                self.docker.restart = restart
                result = self.call(api_factory=rc.API, validate_fn=activation_plan.validate_effective, readback_fn=activation.readback)
                self.assertEqual(result['result'], 'passed'); self.assertEqual(self.docker.restarts, 1)
                self.assertEqual(len(result['ops_readback']['resources']), 7)
                self.assertEqual(result['ops_readback']['alert_sources'], ['prometheus', 'alertmanager'])
                self.assertEqual(result['cache']['baseline_entries_preserved'], 2)
                self.assertGreater(result['cache']['current_entries'], 2)
                rc.API(manage.existing(self.f.directory)).expired(old_cookie)
                self.assertTrue(all(method == 'GET' and auth is None for method, path, auth in fixture.calls))
                expressions = [parse_qs(urlsplit(path).query)['query'][0] for _, path, _ in fixture.calls if path.startswith('/api/v1/query?')]
                self.assertTrue(any('discovered-machine' in expression and '/srv/discovered' in expression for expression in expressions))
                self.assertTrue(any(path.startswith('/api/v2/alerts?') for _, path, _ in fixture.calls))
            finally: stop()


if __name__ == '__main__': unittest.main()
