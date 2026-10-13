"""Repeatable discovery refresh: owned fields, durable lineage and real HTTP restart."""
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
from ops_platform.ops import activate_discovery as activation, activation_plan, discover, refresh_discovery as refresh
from ops_platform.ops import manage, restart_check as rc, set_public_url as change
from ops_platform.ops.discovery_compose import ROLES
from ops_platform.tests.test_discovery import Fixture as HTTPFixture, DockerFixture
from ops_platform.tests import test_discovery_activation as activation_tests
from ops_platform.tests.test_restart_check import IDENT


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.h = activation_tests.ActivationTests(); self.h.setUp(); self.addCleanup(self.h.doCleanups)
        self.h.call(); self.h.validated.clear(); self.h.reads.clear()
        self.directory = self.h.f.directory; self.docker = self.h.docker
        self.anchor = (self.directory / 'discovery-activation/baseline.json').read_bytes()
        self.original_configs = self.h.config_snapshot()
        self.failure = None
        def restart(ident):
            paths = sorted((self.directory / 'discovery-refresh').glob('*/baseline.json'))
            record = activation.load_record(paths[-1], self.directory)
            intent = record['rollback']['restart_intent'] if record.get('rollback') else record['restart_intent']
            self.assertEqual(intent['id'], ident)
            self.docker.restarts += 1
            if self.failure == 'before': raise ReadFailure('DOCKER_TIMEOUT')
            self.docker.raw['started'] = rc.now(); self.h.f.api.sessions.clear()
            self.h.mounted = rc.read_file(self.directory / 'config.json')[1]['sha256']
            if self.docker.after_restart: self.docker.after_restart()
            if self.failure == 'after': raise ReadFailure('DOCKER_TIMEOUT')
        self.docker.restart = restart

    def call(self, action='apply', **kwargs):
        options = dict(docker=self.docker, discover_fn=lambda cfg: deepcopy(self.h.report),
                       validate_fn=self.h.validate, source_fn=lambda: self.h.source,
                       api_factory=lambda cfg: self.h.f.api, readback_fn=self.h.readback, wait_seconds=0)
        options.update(kwargs)
        return refresh.run(action, self.directory, **options)

    def error(self, code, action='apply', **kwargs):
        with self.assertRaises(ReadFailure) as caught: self.call(action, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def changed(self, suffix='next'):
        self.h.report['metrics']['generated_profile']['sources']['host']['job'] = 'host-' + suffix
        self.h.report['metrics']['generated_profile']['target_jobs'][0] = 'host-' + suffix
        self.h.report['metrics']['generated_profile']['filesystems']['mountpoint_include'] = '/|/srv/' + suffix
        self.h.report['connection_overlay']['alertmanager_url'] = 'http://127.0.0.1:' + ('28321' if suffix == 'next' else '28322')

    def journals(self): return sorted((self.directory / 'discovery-refresh').glob('*/baseline.json'))

    def test_unchanged_refresh_is_live_without_restart_or_new_journal(self):
        result = self.call()
        self.assertEqual(result['result'], 'passed'); self.assertFalse(result['configuration_changed'])
        self.assertFalse(result['restart_requested']); self.assertFalse(result['provenance_journal_written'])
        self.assertIsNone(result['generation']); self.assertFalse(result['evidence_reused'])
        self.assertTrue(result['live_checks_performed']); self.assertEqual(len(self.h.reads), 1)
        self.assertEqual(self.h.config_snapshot(), self.original_configs); self.assertFalse(self.journals())
        self.assertEqual(self.docker.restarts, 1)
        self.assertEqual((self.directory / 'discovery-activation/baseline.json').read_bytes(), self.anchor)

    def test_changed_owned_address_job_mount_are_applied_and_second_cycle_is_noop(self):
        self.changed(); result = self.call()
        self.assertEqual(result['generation'], 1); self.assertEqual(result['changed_fields'], ['alertmanager_url', 'resource_profile'])
        self.assertTrue(result['unmanaged_values_preserved']); self.assertNotIn('existing_values_preserved', result)
        cfg = manage.existing(self.directory)
        self.assertEqual(cfg['resource_profile']['sources']['host']['job'], 'host-next')
        self.assertTrue(cfg['alertmanager_url'].endswith(':28321'))
        for key, val in json.loads(self.original_configs['host-config.json']).items():
            if key not in ('alertmanager_url', 'resource_profile'): self.assertEqual(cfg[key], val)
        self.assertEqual(self.docker.restarts, 2); self.assertEqual(len(self.journals()), 1)
        self.assertFalse(self.call()['configuration_changed']); self.assertEqual(self.docker.restarts, 2)
        self.assertEqual((self.directory / 'discovery-activation/baseline.json').read_bytes(), self.anchor)

    def test_next_generation_uses_last_accepted_values_and_preserves_ancestors(self):
        self.changed(); self.call(); previous = self.journals()[0].read_bytes()
        self.changed('later'); result = self.call()
        self.assertEqual(result['generation'], 2); self.assertEqual(self.docker.restarts, 3)
        self.assertEqual(self.journals()[0].read_bytes(), previous)
        self.assertEqual(refresh.lineage(self.directory)['fields']['resource_profile']['value']['sources']['host']['job'], 'host-later')

    def test_service_recreation_without_setting_change_records_metadata_without_restart(self):
        self.h.source = 'same-service-new-container-id'
        result = self.call()
        self.assertTrue(result['deployment_metadata_changed']); self.assertFalse(result['configuration_changed'])
        self.assertEqual(result['generation'], 1); self.assertFalse(result['restart_requested'])
        self.assertFalse(self.call()['deployment_metadata_changed']); self.assertEqual(self.docker.restarts, 1)

    def test_manual_edit_releases_ownership_and_is_never_taken_back_by_matching_value(self):
        original_url = manage.existing(self.directory)['alertmanager_url']
        self.h.replace_configs(lambda cfg: cfg.update(alertmanager_url='http://127.0.0.1:29333'))
        result = self.call(); self.assertEqual(result['manual_overrides_detected'], ['alertmanager_url'])
        self.assertEqual(refresh.lineage(self.directory)['fields']['alertmanager_url']['source'], 'user_override')
        self.assertEqual(manage.existing(self.directory)['alertmanager_url'], 'http://127.0.0.1:29333')
        self.assertNotIn('alertmanager_url', result['managed_fields'])
        self.h.replace_configs(lambda cfg: cfg.update(alertmanager_url=original_url)); self.call()
        self.h.report['connection_overlay']['alertmanager_url'] = 'http://127.0.0.1:28321'
        result = self.call()
        self.assertFalse(result['configuration_changed'])
        self.assertEqual(manage.existing(self.directory)['alertmanager_url'], original_url)
        self.assertIn('alertmanager_url', result['preserved_overrides_differing_from_discovery'])

    def test_manual_profile_is_preserved_while_other_managed_value_updates(self):
        def manual(cfg): cfg['resource_profile']['filesystems']['mountpoint_include'] = '/manual'
        self.h.replace_configs(manual); self.changed()
        result = self.call()
        self.assertEqual(result['changed_fields'], ['alertmanager_url'])
        self.assertEqual(result['manual_overrides_detected'], ['resource_profile'])
        self.assertEqual(manage.existing(self.directory)['resource_profile']['filesystems']['mountpoint_include'], '/manual')

    def test_legacy_url_is_not_adopted_even_when_new_discovery_differs(self):
        original = manage.existing(self.directory)['platforms']['model']['url']
        self.h.report['connection_overlay']['platforms']['model']['url'] = 'http://127.0.0.1:29444'
        result = self.call()
        self.assertIn('platforms.model.url', result['preserved_overrides_differing_from_discovery'])
        self.assertEqual(self.h.validated[-1]['platforms']['model']['url'], original)
        self.assertEqual(self.docker.restarts, 1); self.assertFalse(result['configuration_changed'])

    def test_managed_identity_or_cluster_change_is_not_silently_adopted(self):
        old = deepcopy(self.h.report)
        self.h.report['services']['alertmanager']['identity']['project'] = 'foreign'
        self.error('DISCOVERY_LOGICAL_IDENTITY_CHANGED'); self.h.report = deepcopy(old)
        self.h.report['services']['prometheus']['identity']['project'] = 'foreign'
        self.error('DISCOVERY_LOGICAL_IDENTITY_CHANGED'); self.h.report = deepcopy(old)
        self.h.report['metrics']['generated_profile']['scope'] = {'cluster': 'foreign'}
        self.h.report['metrics']['generated_profile']['default_cluster'] = 'foreign'
        self.error('RESOURCE_SCOPE_CHANGED'); self.assertFalse(self.journals())
        self.assertEqual(self.h.config_snapshot(), self.original_configs)

    def test_manual_prometheus_override_cannot_receive_other_servers_generated_profile(self):
        self.h.replace_configs(lambda cfg: cfg.update(prometheus_url='http://127.0.0.1:29222'))
        before = self.h.config_snapshot(); self.error('PROFILE_SOURCE_CONFLICT')
        self.assertEqual(self.h.config_snapshot(), before); self.assertEqual(self.docker.restarts, 1)

    def test_removed_field_is_not_refilled_without_ownership(self):
        self.h.replace_configs(lambda cfg: cfg.pop('alertmanager_url'))
        before = self.h.config_snapshot(); self.error('PROVENANCE_FIELD_REMOVED')
        self.assertEqual(self.h.config_snapshot(), before); self.assertFalse(self.journals())

    def test_unavailable_discovery_or_bad_effective_profile_leaves_last_config(self):
        self.changed(); self.h.report['result'] = 'attention_required'; self.error('VERIFIED_DISCOVERY_REQUIRED')
        self.h.report['result'] = 'discovered'
        def fail(cfg): raise ReadFailure('EFFECTIVE_PROFILE_NOT_READY')
        self.error('EFFECTIVE_PROFILE_NOT_READY', validate_fn=fail)
        self.assertEqual(self.h.config_snapshot(), self.original_configs); self.assertFalse(self.journals())
        self.assertEqual(self.docker.restarts, 1)

    def test_partial_pair_resumes_single_generation(self):
        self.changed(); write = activation.atomic_config
        def fail(path, *args):
            if path.name == 'config.json': raise OSError('synthetic disk failure')
            return write(path, *args)
        with patch.object(activation, 'atomic_config', side_effect=fail):
            with self.assertRaises(OSError): self.call()
        self.assertEqual(self.docker.restarts, 1)
        self.assertEqual(self.call()['result'], 'passed'); self.assertEqual(self.docker.restarts, 2)
        self.assertEqual(len(self.journals()), 1)

    def test_initial_journal_failure_leaves_empty_directory_reusable(self):
        self.changed()
        with patch.object(rc, 'write_record', side_effect=OSError('synthetic fsync failure')):
            with self.assertRaises(OSError): self.call()
        self.assertEqual(self.h.config_snapshot(), self.original_configs); self.assertEqual(self.docker.restarts, 1)
        self.assertEqual(self.call()['generation'], 1); self.assertEqual(len(self.journals()), 1)

    def test_lost_restart_response_after_effect_is_checked_without_duplicate(self):
        self.changed(); self.failure = 'after'; self.error('DOCKER_TIMEOUT')
        result = self.call('check')
        self.assertEqual(result['result'], 'passed'); self.assertFalse(result['restart_command_completion_recorded'])
        self.assertEqual(self.docker.restarts, 2); self.assertTrue(self.call('check')['evidence_reused'])

    def test_uncertain_restart_without_effect_is_not_replayed(self):
        self.changed(); self.failure = 'before'; self.error('DOCKER_TIMEOUT')
        self.error('OPS_ACTIVATION_NOT_READY'); self.error('RESTART_EFFECT_UNKNOWN_CHECK_FIRST', 'rollback')
        self.assertEqual(self.docker.restarts, 2)

    def test_changed_provenance_or_source_during_capture_stops_before_writes(self):
        self.changed()
        def drift(cfg):
            p = self.directory / 'discovery-activation/baseline.json'
            body = json.loads(p.read_text()); body['report']['checked_at'] = 'changed'; rc.write_record(p, body)
            return self.h.report
        self.error('PROVENANCE_RECORD_CHANGED', discover_fn=drift)
        self.assertEqual(self.h.config_snapshot(), self.original_configs); self.assertFalse(self.journals())

    def test_accepted_ancestor_mutation_breaks_lineage(self):
        self.changed(); self.call(); self.changed('later'); self.call()
        p = self.journals()[0]; body = json.loads(p.read_text()); body['report']['result'] = 'altered'; rc.write_record(p, body)
        self.error('REFRESH_HISTORY_INVALID'); self.assertEqual(self.docker.restarts, 3)

    def test_source_change_before_install_requires_recovery_without_writes(self):
        self.changed(); calls = 0
        def source():
            nonlocal calls
            calls += 1
            return self.h.source if calls <= 2 else 'replaced'
        self.error('DISCOVERY_BINDING_CHANGED', source_fn=source)
        self.assertEqual(self.h.config_snapshot(), self.original_configs); self.assertEqual(self.docker.restarts, 1)
        self.assertEqual(self.call('rollback')['result'], 'rolled_back'); self.assertEqual(self.docker.restarts, 1)

    def test_post_restart_readback_failure_remains_pending_and_logs_out(self):
        self.changed()
        def fail(cfg): raise ReadFailure('OPS_ALERT_READBACK_FAILED')
        self.error('OPS_ALERT_READBACK_FAILED', readback_fn=fail)
        self.assertTrue(refresh.lineage(self.directory)['pending']); self.assertFalse(self.h.f.api.sessions)
        self.assertEqual(self.call('check')['result'], 'passed'); self.assertEqual(self.docker.restarts, 2)

    def test_rollback_restores_original_and_same_proposal_requires_explicit_retry(self):
        self.changed(); self.call()
        self.assertEqual(self.call('rollback')['result'], 'rolled_back'); self.assertEqual(self.docker.restarts, 3)
        self.assertEqual(self.h.config_snapshot(), self.original_configs)
        self.assertTrue(self.call('rollback')['evidence_reused']); self.error('ROLLED_BACK_PROPOSAL_REQUIRES_REVIEW')
        self.assertTrue(self.call('plan')['rolled_back_proposal_blocked'])
        self.assertEqual(self.call(retry_rolled_back=True)['generation'], 2); self.assertEqual(self.docker.restarts, 4)

    def test_rollback_response_loss_resumes_once(self):
        self.changed(); self.call(); self.failure = 'after'; self.error('DOCKER_TIMEOUT', 'rollback')
        self.failure = None
        self.assertEqual(self.call('rollback')['result'], 'rolled_back'); self.assertEqual(self.docker.restarts, 3)

    def test_plan_is_read_only_and_history_is_bounded_and_symlinks_refused(self):
        self.changed(); result = self.call('plan')
        self.assertEqual(result['result'], 'planned'); self.assertFalse(self.journals())
        self.assertEqual(self.h.config_snapshot(), self.original_configs)
        folder = self.directory / 'discovery-refresh'; folder.symlink_to(self.directory / 'restart-check')
        self.error('SYMLINK_REFUSED'); folder.unlink()
        with patch.object(refresh, 'MAX_GENERATIONS', 0): self.error('REFRESH_HISTORY_LIMIT')
        with change.change_lock(self.directory): self.error('PUBLIC_URL_CHANGE_BUSY')
        self.assertEqual(self.docker.restarts, 1)

    def test_watch_two_unchanged_cycles_has_no_restart_or_journal(self):
        output, sleeps = [], []
        code = refresh.watch(self.directory, 30, 2, run_fn=lambda action, path: self.call(action), emit=output.append, sleep=sleeps.append)
        self.assertEqual(code, 0); self.assertEqual(len(output), 2); self.assertEqual(sleeps, [30])
        self.assertTrue(all(row['watch_enabled'] and not row['restart_requested'] for row in output))
        self.assertFalse(self.journals()); self.assertEqual(self.docker.restarts, 1)

    def test_watch_recovers_preflight_failure_but_stops_on_pending_restart(self):
        output = []; count = 0
        def cycle(action, path):
            nonlocal count
            count += 1
            if count == 1: raise ReadFailure('VERIFIED_DISCOVERY_REQUIRED')
            return self.call()
        self.assertEqual(refresh.watch(self.directory, 30, 2, run_fn=cycle, emit=output.append, sleep=lambda _: None), 0)
        self.assertFalse(output[0]['watch_stopped_for_recovery']); self.assertEqual(output[1]['result'], 'passed')
        self.changed(); self.failure = 'after'; output = []
        self.assertEqual(refresh.watch(self.directory, 30, 3, run_fn=lambda action, path: self.call(), emit=output.append, sleep=lambda _: self.fail('must stop')), 2)
        self.assertEqual(len(output), 1); self.assertTrue(output[0]['watch_stopped_for_recovery'])
        self.assertEqual(self.docker.restarts, 2)

    def test_real_discovery_http_job_and_endpoint_change_restarts_web_once_then_noops(self):
        h = activation_tests.ActivationTests(); h.setUp(); self.addCleanup(h.doCleanups)
        first, second = HTTPFixture(), HTTPFixture(); self.addCleanup(first.close); self.addCleanup(second.close)
        for fixture in (first, second):
            original_response = fixture.response
            def response(path, fixture=fixture, original_response=original_response):
                if path.startswith('/api/v1/rules?'):
                    return {'status': 'success', 'data': {'groups': [{'name': GROUP, 'rules': [
                        {'name': name, 'type': 'alerting', 'duration': duration, 'labels': {'scope': SCOPE},
                         'state': 'inactive', 'health': 'ok', 'lastEvaluation': rc.now(), 'alerts': []}
                        for name, (_, duration) in RULES.items()]}]}}
                if path.startswith('/api/v2/alerts?'): return []
                body = original_response(path)
                if path.startswith('/api/v1/query?') and parse_qs(urlsplit(path).query)['query'][0].startswith('up{'):
                    body['data']['result'] = [fixture.sample(fixture.labels(role)) for role in fixture.jobs]
                return body
            fixture.response = response
        def initial(cfg):
            cfg['prometheus_url'] = first.url; cfg['langfuse_url'] = first.url
            for role in cfg['platforms']: cfg['platforms'][role]['url'] = first.url
        h.replace_configs(initial)
        rows = []
        for n, (role, (service, subtree)) in enumerate(ROLES.items()):
            rows.append({'id': format(n+1,'064x'), 'image': 'sha256:'+'b'*64, 'state': 'running',
                         'started_at': '2026-09-28T00:00:00Z', 'project': 'fixture-'+role, 'service': service,
                         'working_dir': str(discover.ROOT / subtree), 'config_files': str(discover.ROOT / subtree / 'compose.yml'),
                         'network_mode': 'bridge', 'ports': {'8000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(first.server.server_port)}]},
                         'listen_flags': [None]})
        discover_fn = lambda cfg: discover.discover(cfg, discover.ROOT, DockerFixture(rows))
        source_fn = lambda: change.digest(rows)
        env = dict(os.environ, OPS_CONFIG=str(h.f.directory / 'host-config.json'),
                   PYTHONPATH=os.pathsep.join(sorted({str(Path(m.__file__).resolve().parents[1]) for m in (fastapi, uvicorn)})))
        process = None
        with (h.f.root / 'process.log').open('w+') as log:
            def stop():
                nonlocal process
                if process is not None and process.poll() is None:
                    process.terminate()
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
            def start():
                nonlocal process
                process = subprocess.Popen([sys.executable, '-B', '-m', 'ops_platform.server'], cwd=discover.ROOT,
                                            env=env, stdout=log, stderr=log)
                deadline = time.monotonic()+10
                while process.poll() is None and time.monotonic()<deadline:
                    try: rc.API(manage.existing(h.f.directory)).health(); return
                    except ReadFailure: time.sleep(.03)
                log.flush(); log.seek(0); self.fail(log.read()[-2000:])
            def restart(ident):
                self.assertEqual(ident, IDENT); h.docker.restarts += 1
                stop(); start(); h.docker.raw['started'] = rc.now()
                h.mounted = rc.read_file(h.f.directory / 'config.json')[1]['sha256']
            try:
                start(); h.docker.restart = restart
                h.call(discover_fn=discover_fn, source_fn=source_fn, api_factory=rc.API,
                       validate_fn=activation_plan.validate_effective, readback_fn=activation.readback)
                anchor = (h.f.directory / 'discovery-activation/baseline.json').read_bytes()
                before = rc.cache(manage.existing(h.f.directory)); old_cookie = rc.API(manage.existing(h.f.directory)).login()
                first.jobs['host'] = 'machine-moved'
                am = rows[list(ROLES).index('alertmanager')]
                am['ports']['8000/tcp'][0]['HostPort'] = str(second.server.server_port)
                options = dict(docker=h.docker, discover_fn=discover_fn, source_fn=source_fn, api_factory=rc.API, wait_seconds=0)
                result = refresh.run('apply', h.f.directory, **options)
                self.assertEqual(result['result'], 'passed'); self.assertEqual(h.docker.restarts, 2)
                self.assertEqual(result['changed_fields'], ['alertmanager_url','resource_profile'])
                cfg = manage.existing(h.f.directory)
                self.assertEqual(cfg['alertmanager_url'], second.url)
                self.assertEqual(cfg['resource_profile']['sources']['host']['job'], 'machine-moved')
                self.assertEqual(len(result['ops_readback']['resources']), 7)
                self.assertEqual(result['cache']['baseline_entries_preserved'], len(before['success_times']))
                rc.API(cfg).expired(old_cookie)
                again = refresh.run('apply', h.f.directory, **options)
                self.assertFalse(again['restart_requested']); self.assertEqual(h.docker.restarts, 2)
                self.assertEqual((h.f.directory / 'discovery-activation/baseline.json').read_bytes(), anchor)
                self.assertTrue(any(path.startswith('/api/v2/alerts?') for _, path, _ in second.calls))
                self.assertTrue(all(method == 'GET' and auth is None for f in (first,second) for method,path,auth in f.calls))
            finally: stop()


if __name__ == '__main__': unittest.main()
