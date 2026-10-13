"""LAN host/origin repair, interruption safety and real web-process verification."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import fastapi
import uvicorn
from ops_platform.contracts import ReadFailure
from ops_platform.ops import manage, restart_check as rc, set_public_url as change
from ops_platform.tests.test_restart_check import Fixture, IDENT


class ChangeTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.url = 'http://192.168.0.119:' + str(self.f.port)
        self.protected = {'alertmanager': 'old-am', 'prometheus': 'old-prom', 'agent': 'old-agent'}
        self.docker = self.f.docker
        self.docker.protected = lambda ident: deepcopy(self.protected)
        self.mounted = rc.read_file(self.f.directory / 'config.json')[1]['sha256']
        self.docker.mounted_hash = lambda ident: self.mounted
        self.failure = None
        old_restart = self.docker.restart
        def restart(ident):
            record = change.read_record(self.evidence, self.f.directory, self.url)
            self.assertTrue(record['restart_intent'])
            self.assertFalse(record['restart_command_completed'])
            if self.failure == 'before':
                self.docker.restarts += 1
                raise ReadFailure('DOCKER_TIMEOUT')
            old_restart(ident)
            self.mounted = rc.read_file(self.f.directory / 'config.json')[1]['sha256']
            if self.failure == 'after': raise ReadFailure('DOCKER_TIMEOUT')
        self.docker.restart = restart
        self.f.api.request = lambda *a: (400, {'error': {'code': 'HOST_NOT_ALLOWED'}}, None)
        self.historical = self.f.directory / 'restart-check/baseline.json'
        self.historical.parent.mkdir(mode=0o700)
        manage.write_new(self.historical, {'report': {'result': 'passed'}, 'original': 'keep'})

    @property
    def evidence(self): return self.f.directory / 'public-url-change/baseline.json'

    def call(self, action='apply', url=None, api_factory=None):
        return change.run(action, self.f.directory, url or self.url, self.docker,
                          api_factory=api_factory or (lambda cfg: self.f.api), wait_seconds=0)

    def assert_error(self, code, **kwargs):
        with self.assertRaises(ReadFailure) as caught: self.call(**kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_success_changes_only_two_urls_preserves_keys_cache_history_and_reuses(self):
        before_files = rc.files(self.f.directory, self.f.cfg)
        original = {name: (self.f.directory / name).read_text() for name in change.CONFIGS}
        cached = self.f.cache.read_bytes()
        history = self.historical.read_bytes()
        report = self.call(url=self.url + '/')
        self.assertEqual(report['result'], 'passed')
        self.assertEqual(report['public_url'], self.url)
        self.assertEqual(self.f.cache.read_bytes(), cached)
        self.assertEqual(self.historical.read_bytes(), history)
        for name in change.CONFIGS:
            expected = dict(json.loads(original[name]), public_url=self.url)
            self.assertEqual(json.loads((self.f.directory / name).read_text()), expected)
        after_files = rc.files(self.f.directory, manage.existing(self.f.directory))
        for name in set(before_files) - set(change.CONFIGS): self.assertEqual(before_files[name], after_files[name])
        self.assertEqual(change.read_record(self.evidence, self.f.directory, self.url)['baseline']['original'], original)
        self.assertEqual(self.docker.restarts, 1)
        self.assertTrue(self.call()['evidence_reused'])
        self.assertEqual(self.docker.restarts, 1)

    def test_lost_restart_response_after_effect_is_read_back_without_second_restart(self):
        self.failure = 'after'
        self.assert_error('DOCKER_TIMEOUT')
        result = self.call('check')
        self.assertEqual(result['result'], 'passed')
        self.assertFalse(result['restart_command_completion_recorded'])
        self.assertEqual(self.docker.restarts, 1)

    def test_lost_restart_response_before_effect_never_reissues(self):
        self.failure = 'before'
        self.assert_error('DOCKER_TIMEOUT')
        self.assert_error('OPS_UPDATE_NOT_READY')
        self.assertEqual(self.docker.restarts, 1)

    def test_partial_config_pair_resumes_from_original_record(self):
        original_write = change.rc.write_record
        def fail_second(path, value):
            if path == self.f.directory / 'config.json': raise OSError('synthetic disk failure')
            return original_write(path, value)
        with patch.object(change.rc, 'write_record', side_effect=fail_second):
            with self.assertRaises(OSError): self.call()
        self.assertEqual(self.docker.restarts, 0)
        with self.assertRaises(ReadFailure): manage.existing(self.f.directory)
        self.assertEqual(self.call()['result'], 'passed')
        self.assertEqual(self.docker.restarts, 1)

    def test_initial_record_write_failure_leaves_configs_unchanged(self):
        original = {name: (self.f.directory / name).read_bytes() for name in change.CONFIGS}
        with patch.object(change.rc, 'write_record', side_effect=OSError('failed fsync')):
            with self.assertRaises(OSError): self.call()
        self.assertEqual(self.docker.restarts, 0)
        for name in change.CONFIGS: self.assertEqual((self.f.directory / name).read_bytes(), original[name])

    def test_check_without_record_and_invalid_target_never_change_configuration(self):
        self.assert_error('NO_PUBLIC_URL_CHANGE_RECORD', action='check')
        for url in ('http://*:' + str(self.f.port), 'http://192.168.0.119:1',
                    'https://192.168.0.119:' + str(self.f.port), 'http://8.8.8.8:' + str(self.f.port)):
            with self.assertRaises(ReadFailure): self.call(url=url)
        self.assertEqual(self.docker.restarts, 0)
        self.assertFalse(self.evidence.exists())

    def test_credential_change_and_tampered_baseline_refuse(self):
        self.failure = 'before'
        self.assert_error('DOCKER_TIMEOUT')
        key = self.f.directory / 'access-key'
        old = key.read_bytes(); key.write_text('new-secret-with-long-enough-value')
        self.assert_error('PUBLIC_URL_CONFIG_OR_CREDENTIAL_DRIFT')
        key.write_bytes(old)
        record = change.read_record(self.evidence, self.f.directory, self.url)
        record['baseline']['target_url'] = 'altered'
        rc.write_record(self.evidence, record)
        self.assert_error('PUBLIC_URL_RECORD_INVALID')
        self.assertEqual(self.docker.restarts, 1)

    def test_old_acceptance_edit_and_new_target_refused(self):
        self.call()
        self.assert_error('PUBLIC_URL_TARGET_DIFFERS', url=self.url.replace('.119:', '.120:'))
        self.historical.write_text('{}')
        self.assert_error('PRIOR_RESTART_RECORD_CHANGED')
        self.assertEqual(self.docker.restarts, 1)

    def test_removed_cache_entry_fails_but_newer_success_is_preserved(self):
        before = rc.cache(self.f.cfg)
        data = json.loads(self.f.cache.read_text())
        data['success_times']['0'*64] = rc.now()
        self.f.cache.write_text(json.dumps(data))
        self.assertFalse(change.cache_guard(self.f.cfg, before)['timestamps_unchanged'])
        del data['success_times']['1'*64]
        self.f.cache.write_text(json.dumps(data))
        with self.assertRaises(ReadFailure) as caught: change.cache_guard(self.f.cfg, before)
        self.assertEqual(caught.exception.code, 'CACHE_ENTRY_LOST_OR_TIME_REVERSED')

    def test_other_service_restart_stale_mount_and_foreign_owner_refused(self):
        self.docker.raw['owner'] = 'foreign'
        self.assert_error('CONTAINER_OWNER_MISMATCH')
        self.docker.raw['owner'] = self.f.owner['owner']
        self.mounted = 'wrong'
        self.assert_error('MOUNTED_CONFIG_DIFFERS')
        self.mounted = rc.read_file(self.f.directory / 'config.json')[1]['sha256']
        self.docker.after_restart = lambda: self.protected.update(alertmanager='changed')
        self.assert_error('OTHER_CONTAINER_CHANGED')
        self.assertEqual(self.docker.restarts, 1)

    def test_symlink_evidence_and_concurrent_change_refused(self):
        folder = self.evidence.parent
        folder.symlink_to(self.historical.parent)
        self.assert_error('SYMLINK_REFUSED')
        folder.unlink()
        with change.change_lock(self.f.directory): self.assert_error('PUBLIC_URL_CHANGE_BUSY')
        self.assertEqual(self.docker.restarts, 0)

    def test_real_web_restart_allows_lan_host_origin_login_and_keeps_unknown_host_blocked(self):
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
                self.assertEqual(ident, IDENT)
                self.docker.restarts += 1
                stop(); start()
                self.docker.raw['started'] = rc.now()
                self.mounted = rc.read_file(self.f.directory / 'config.json')[1]['sha256']
            try:
                start()
                rejected = rc.API(dict(self.f.cfg, public_url=self.url)).request('GET', '/healthz')
                self.assertEqual(rejected[1]['error']['code'], 'HOST_NOT_ALLOWED')
                old_cookie = rc.API(self.f.cfg).login()
                self.docker.restart = restart
                result = self.call(api_factory=rc.API)
                self.assertEqual(result['result'], 'passed')
                self.assertTrue(result['same_access_code_login_and_logout'])
                self.assertTrue(result['unconfigured_host_rejected'])
                rc.API(manage.existing(self.f.directory)).expired(old_cookie)
                self.assertEqual(self.docker.restarts, 1)
            finally: stop()


if __name__ == '__main__': unittest.main()
