"""Fresh-install discovery, atomic publication and refresh with real HTTP fixtures."""
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import fastapi
import uvicorn
from ops_platform.alerts import GROUP, RULES, SCOPE
from ops_platform.contracts import ReadFailure
from ops_platform.ops import bootstrap_discovery as bootstrap, manage, discover
from ops_platform.ops import activate_discovery as activation, refresh_discovery as refresh
from ops_platform.ops import restart_check as rc, set_public_url as change
from ops_platform.ops.discovery_compose import ROLES, scoped
from ops_platform.tests import test_discovery as discovery_tests
from ops_platform.tests.test_restart_check import FakeDocker, FakeAPI, IDENT, TOKEN, LF_SECRET

PROJECT = Path(__file__).resolve().parents[2]


def enrich(http):
    original = http.response
    def response(path):
        if path.startswith('/api/v1/rules?'):
            return {'status': 'success', 'data': {'groups': [{'name': GROUP, 'rules': [
                {'name': name, 'type': 'alerting', 'duration': duration, 'labels': {'scope': SCOPE},
                 'state': 'inactive', 'health': 'ok', 'lastEvaluation': rc.now(), 'alerts': []}
                for name, (_, duration) in RULES.items()]}]}}
        if path.startswith('/api/v2/alerts?'): return []
        body = original(path)
        if path.startswith('/api/v1/query?') and parse_qs(urlsplit(path).query)['query'][0].startswith('up{'):
            body['data']['result'] = [http.sample(http.labels(role)) for role in http.jobs]
        return body
    http.response = response


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.h = discovery_tests.DiscoveryTests(); self.h.setUp(); self.addCleanup(self.h.doCleanups)
        self.root, self.http, self.rows = self.h.root, self.h.fixture, self.h.rows
        enrich(self.http)
        folder = self.root / 'agent_platform/.secrets'; folder.mkdir(parents=True, mode=0o700)
        self.token = folder / 'api-token'; manage.write_new(self.token, TOKEN)
        self.lf = folder / 'langfuse-tracing.json'
        manage.write_new(self.lf, {'project_id': 'synthetic', 'public_key': 'synthetic', 'secret_key': LF_SECRET})
        self.directory = self.root / 'ops-state'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); self.port = sock.getsockname()[1]
        self.url = 'http://localhost:' + str(self.port)
        self.docker = discovery_tests.DockerFixture(self.rows)
        for mocker in (patch.object(manage, 'ROOT', self.root / 'ops_platform'),
                       patch.object(bootstrap, 'DockerDiscovery', return_value=self.docker)):
            mocker.start(); self.addCleanup(mocker.stop)

    def prepare(self): return manage.prepare(self.directory, self.url)

    def snapshot(self):
        return {str(p.relative_to(self.directory)): p.read_bytes() for p in self.directory.rglob('*') if p.is_file()}

    def error(self, code, fn=None):
        with self.assertRaises(ReadFailure) as caught: (fn or self.prepare)()
        self.assertEqual(caught.exception.code, code)

    def test_first_prepare_discovers_all_fields_and_private_provenance_without_port_defaults(self):
        result = self.prepare(); cfg = manage.existing(self.directory)
        self.assertEqual(result['result'], 'prepared'); self.assertEqual(len(result['managed_fields']), 8)
        for key in bootstrap.URLS: self.assertEqual(bootstrap.value(cfg, key), self.http.url)
        self.assertEqual(cfg['resource_profile']['sources']['host']['job'], self.http.jobs['host'])
        self.assertIn('srv/data', cfg['resource_profile']['filesystems']['mountpoint_include'])
        record = bootstrap.load_anchor(self.directory)
        self.assertEqual(record['phase'], 'configuration_prepared')
        self.assertNotIn('report', record); self.assertNotIn('accepted', record['runtime_acceptance'])
        for p in self.directory.rglob('*'): self.assertEqual(p.stat().st_mode & 0o777, 0o700 if p.is_dir() else 0o600)
        for secret in (TOKEN, LF_SECRET, (self.directory/'access-key').read_text().strip()):
            self.assertNotIn(secret, json.dumps(record)); self.assertNotIn(secret, json.dumps(result))
        self.assertTrue(all(method == 'GET' and auth is None for method, path, auth in self.http.calls))
        self.assertFalse((self.directory / 'discovery-activation').exists())

    def test_existing_prepare_reuses_offline_without_changing_key_configs_or_provenance(self):
        self.prepare(); before = self.snapshot(); calls = len(self.http.calls)
        with patch.object(bootstrap, 'propose', side_effect=AssertionError('unexpected discovery')):
            self.assertEqual(manage.prepare(self.directory)['result'], 'reused')
            self.error('EXISTING_PUBLIC_URL_DIFFERS', lambda: manage.prepare(self.directory, 'http://localhost:19001'))
        self.assertEqual(before, self.snapshot()); self.assertEqual(calls, len(self.http.calls))

    def test_public_url_prompt_remains_only_browser_address_input(self):
        with patch('builtins.input', return_value=self.url) as prompt:
            self.assertEqual(manage.prepare(self.directory)['result'], 'prepared')
        prompt.assert_called_once(); self.assertEqual(manage.existing(self.directory)['public_url'], self.url)

    def test_explicit_credential_paths_are_preserved(self):
        token, lf = self.root / 'key', self.root / 'credentials.json'
        manage.write_new(token, TOKEN); manage.write_new(lf, self.lf.read_text())
        self.token.unlink(); self.lf.unlink()
        manage.prepare(self.directory, self.url, token, lf)
        cfg = manage.existing(self.directory)
        self.assertEqual(cfg['platforms']['agent']['token_file'], str(token)); self.assertEqual(cfg['langfuse_file'], str(lf))

    def test_missing_key_prevents_probes_and_new_state(self):
        self.token.unlink()
        with self.assertRaises((ReadFailure, OSError)): self.prepare()
        self.assertFalse(self.directory.exists()); self.assertFalse(self.http.calls)

    def test_ambiguous_service_refuses_without_fallback_or_partial_state(self):
        other = deepcopy(self.rows[0]); other['id'] = 'f' * 64; self.rows.append(other)
        self.error('FIRST_INSTALL_DISCOVERY_NOT_READY'); self.assertFalse(self.directory.exists())

    def test_missing_service_refuses_without_fallback(self):
        self.rows.pop()
        self.error('FIRST_INSTALL_DISCOVERY_NOT_READY'); self.assertFalse(self.directory.exists())

    def test_stale_metric_roles_refuse_without_fixed_profile(self):
        self.http.stale = True
        self.error('FIRST_INSTALL_DISCOVERY_NOT_READY'); self.assertFalse(self.directory.exists())

    def test_metadata_change_during_discovery_refuses_publication(self):
        self.docker.changed = True
        self.error('DISCOVERY_BINDING_CHANGED'); self.assertFalse(self.directory.exists())

    def test_metadata_change_before_publication_discards_stage(self):
        propose = bootstrap.propose
        def changed(*args):
            result = propose(*args); self.rows[0]['started_at'] = 'changed'; return result
        with patch.object(bootstrap, 'propose', side_effect=changed): self.error('DISCOVERY_BINDING_CHANGED')
        self.assertFalse(self.directory.exists()); self.assertFalse(list(self.root.glob('.ops-prepare-*')))

    def test_write_failure_does_not_leave_an_incomplete_installation_and_retry_succeeds(self):
        write = manage.write_new
        def fail(path, *args):
            if path.name == 'compose.env': raise OSError('fixture write failure')
            return write(path, *args)
        with patch.object(manage, 'write_new', side_effect=fail):
            with self.assertRaises(OSError): self.prepare()
        self.assertFalse(self.directory.exists()); self.assertFalse(list(self.root.glob('.ops-prepare-*')))
        self.assertEqual(self.prepare()['result'], 'prepared')

    def test_concurrent_destination_is_never_replaced_even_if_empty(self):
        publish = bootstrap.publish_no_replace
        def racing(stage, destination):
            destination.mkdir(mode=0o700); publish(stage, destination)
        with patch.object(bootstrap, 'publish_no_replace', side_effect=racing): self.error('PREPARE_DESTINATION_EXISTS')
        self.assertTrue(self.directory.is_dir()); self.assertEqual(list(self.directory.iterdir()), [])
        self.assertFalse(list(self.root.glob('.ops-prepare-*')))

    def test_symlink_destination_is_refused(self):
        self.directory.symlink_to(self.root/'absent', target_is_directory=True)
        self.error('SYMLINK_REFUSED'); self.assertFalse((self.root/'absent').exists())

    def test_anchor_tamper_symlink_and_owner_mismatch_are_refused(self):
        self.prepare(); path = self.directory / bootstrap.RELATIVE; original = path.read_bytes()
        record = json.loads(original); record['baseline']['fields']['prometheus_url']['value'] = 'http://changed:1234'
        path.write_text(json.dumps(record)); self.error('BOOTSTRAP_RECORD_INVALID', lambda: refresh.lineage(self.directory))
        path.write_bytes(original); path.unlink(); path.symlink_to(self.token)
        self.error('SYMLINK_REFUSED', lambda: refresh.lineage(self.directory)); path.unlink(); path.write_bytes(original); path.chmod(0o600)
        owner = self.directory/'owner.json'; changed = json.loads(owner.read_text()); changed['owner'] = 'different'; owner.write_text(json.dumps(changed))
        self.error('BOOTSTRAP_RECORD_INVALID', lambda: refresh.lineage(self.directory))

    def test_initial_provenance_is_refreshable_but_never_legacy_activated(self):
        self.prepare(); context = refresh.lineage(self.directory)
        self.assertEqual(len(context['fields']), 8); self.assertFalse(context['records'])
        self.assertTrue(all(f['automatic_future_update_allowed'] for f in context['fields'].values()))
        self.error('BOOTSTRAP_USE_REFRESH', lambda: activation.run('apply', self.directory))
        folder = self.directory/'discovery-activation'; folder.mkdir(mode=0o700); manage.write_new(folder/'baseline.json', {})
        self.error('MULTIPLE_DISCOVERY_ANCHORS', lambda: refresh.lineage(self.directory))

    def test_plan_existing_installation_is_read_only_and_not_runtime_acceptance(self):
        self.prepare(); before = self.snapshot(); result = bootstrap.plan(self.directory)
        self.assertEqual(result['result'], 'passed'); self.assertTrue(result['existing_preparation_reused'])
        self.assertTrue(result['existing_prepared_state_unchanged']); self.assertEqual(before, self.snapshot())
        self.assertFalse(result['configuration_written']); self.assertFalse(result['new_install_runtime_acceptance'])
        self.assertEqual(len(result['resource_validation']), 7)

    def test_plan_without_prepared_state_creates_no_files(self):
        result = bootstrap.plan(self.directory)
        self.assertEqual(result['result'], 'passed'); self.assertFalse(self.directory.exists())
        self.assertFalse(result['existing_preparation_reused'])

    def test_concurrent_config_change_is_not_reported_as_preserved(self):
        self.prepare()
        def change_config(root):
            result = bootstrap.propose(root)
            with (self.directory/'compose.env').open('a') as out: out.write('# changed\n')
            return result
        self.error('PREPARED_STATE_CHANGED_DURING_PLAN', lambda: bootstrap.plan(self.directory, change_config))

    def test_legacy_state_is_not_migrated_by_first_install_plan(self):
        from ops_platform.tests.test_restart_check import Fixture
        f = Fixture(); self.addCleanup(f.close)
        before = bootstrap.state_stamp(f.directory)
        result = bootstrap.plan(f.directory)
        self.assertTrue(result['existing_preparation_reused']); self.assertEqual(before, bootstrap.state_stamp(f.directory))
        self.assertFalse((f.directory / 'discovery-bootstrap').exists())
        self.assertNotIn('resource_profile', manage.existing(f.directory))

    def test_invalid_initial_anchor_prevents_build_or_start(self):
        self.prepare(); (self.directory/bootstrap.RELATIVE).write_text('{}')
        with patch.object(manage, 'docker', side_effect=AssertionError('must not control Docker')):
            with self.assertRaises(KeyError): manage.compose(self.directory, 'up')

    def test_owned_gateway_manual_override_is_preserved_and_released(self):
        from ops_platform.ops import refresh_plan
        self.prepare(); cfg = manage.existing(self.directory)
        cfg['platforms']['model']['url'] = 'http://manual-gateway:23456'
        discovered = discover.discover(cfg, self.root, self.docker)
        desired, plan = refresh_plan.merge(cfg, discovered, bootstrap.load_anchor(self.directory)['baseline']['fields'])
        self.assertEqual(desired['platforms']['model']['url'], cfg['platforms']['model']['url'])
        self.assertEqual(plan['fields']['platforms.model.url']['source'], 'user_override')
        self.assertNotIn('platforms.model.url', plan['managed_fields'])
        self.assertEqual(len(plan['managed_fields']), 7)

    def test_discovery_failure_reports_role_without_upstream_private_data(self):
        self.rows.pop(0)
        with self.assertRaises(bootstrap.DiscoveryNotReady) as caught: self.prepare()
        self.assertEqual(caught.exception.details['services']['model']['error'], 'SERVICE_MISSING')
        self.assertNotIn('synthetic-private-sentinel', json.dumps(caught.exception.details))

    def test_new_start_warms_cache_and_refreshes_gateway_port_and_profile_with_real_web(self):
        self.prepare(); cfg = manage.existing(self.directory)
        anchor = (self.directory / bootstrap.RELATIVE).read_bytes()
        owner = json.loads((self.directory/'owner.json').read_text())
        fake = SimpleNamespace(cfg=cfg, directory=self.directory, owner=owner, api=FakeAPI(cfg))
        docker = FakeDocker(fake); docker.protected = lambda ident: {'fixture-protected': 'unchanged'}
        mounted = rc.read_file(self.directory/'config.json')[1]['sha256']
        docker.mounted_hash = lambda ident: mounted
        process = None
        env = dict(os.environ, OPS_CONFIG=str(self.directory/'host-config.json'),
                   PYTHONPATH=os.pathsep.join(sorted({str(Path(m.__file__).resolve().parents[1]) for m in (fastapi, uvicorn)})))
        with (self.root/'process.log').open('w+') as log:
            def stop():
                nonlocal process
                if process is not None and process.poll() is None:
                    process.terminate()
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
            def start():
                nonlocal process
                process = subprocess.Popen([sys.executable,'-B','-m','ops_platform.server'], cwd=PROJECT, env=env, stdout=log, stderr=log)
                deadline = time.monotonic()+10
                while process.poll() is None and time.monotonic()<deadline:
                    try: rc.API(manage.existing(self.directory)).health(); return
                    except ReadFailure: time.sleep(.03)
                log.flush(); log.seek(0); self.fail(log.read()[-2000:])
            commands = []
            def command(args, capture=False):
                commands.append(args)
                if args[0] == 'context': return 'unix:///fixture'
                if 'ps' in args: return ''
                self.assertEqual(args[-8:], ['up','--build','-d','--no-deps','--wait','--wait-timeout','60','ops-web'])
                start(); return ''
            def restart(ident):
                nonlocal mounted
                self.assertEqual(ident, IDENT); docker.restarts += 1; stop(); start()
                docker.raw['started'] = rc.now(); mounted = rc.read_file(self.directory/'config.json')[1]['sha256']
            try:
                with patch.object(manage, 'docker', side_effect=command): result = manage.compose(self.directory, 'up')
                self.assertEqual(result['first_install_readback'], 'passed'); self.assertEqual(len(result['ops_readback']['resources']), 7)
                self.assertGreater(len(rc.cache(cfg)['success_times']), 0)
                docker.restart = restart
                options = dict(docker=docker, discover_fn=lambda cfg: discover.discover(cfg, self.root, self.docker),
                               source_fn=lambda: change.digest(scoped(self.rows, self.root)), wait_seconds=0)
                result = refresh.run('apply', self.directory, **options)
                self.assertFalse(result['restart_requested']); self.assertFalse(result['provenance_journal_written'])
                self.assertEqual(len(result['managed_fields']), 8)
                second = discovery_tests.Fixture(); self.addCleanup(second.close)
                self.rows[0]['ports']['8000/tcp'][0]['HostPort'] = str(second.server.server_port)
                self.http.jobs['host'] = 'host-relocated'
                cookie = rc.API(cfg).login()
                result = refresh.run('apply', self.directory, **options)
                self.assertEqual(result['changed_fields'], ['platforms.model.url','resource_profile'])
                self.assertEqual(docker.restarts, 1); self.assertEqual(result['result'], 'passed')
                rc.API(manage.existing(self.directory)).expired(cookie)
                again = refresh.run('apply', self.directory, **options)
                self.assertFalse(again['restart_requested']); self.assertEqual(docker.restarts, 1)
                self.assertEqual((self.directory/bootstrap.RELATIVE).read_bytes(), anchor)
                self.assertFalse((self.directory/'discovery-activation').exists())
            finally: stop()


if __name__ == '__main__': unittest.main()
