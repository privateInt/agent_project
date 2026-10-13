"""Legacy hash compatibility without changing a saved server baseline."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

import test_restart_check as fixtures
from test_langfuse import common, manage
import restart_check as restart


class RestartIdentityTests(unittest.TestCase):
    fixture = fixtures.RestartTests.fixture
    execute = fixtures.RestartTests.execute

    def legacy_baseline(self, runtime):
        # Model the exact v1 file format already written by the old server tool.
        saved = common.read_json(runtime.baseline_path)
        for item in saved['baseline']['containers'].values():
            item.pop('mounts_order_sha256', None)
        saved['baseline_sha256'] = common.fingerprint(saved['baseline'])
        common.write_private(runtime.baseline_path, saved)
        return runtime.baseline_path.read_bytes()

    def test_legacy_mount_order_proof_diagnose_compare_and_repeat_preserve_original(self):
        with self.fixture() as (r, d, server):
            d.after = lambda: d.items['lf-clickhouse']['Mounts'].reverse()
            self.assertEqual(self.execute('run', r)['result'], 'passed')
            baseline = self.legacy_baseline(r)
            trace_before = r.trace_path.read_bytes()
            result = self.execute('diagnose', r)
            row = result['services']['lf-clickhouse']
            self.assertEqual(row['changed_fields'], ['mounts_sha256'])
            self.assertTrue(row['mount_order_equivalence_verified'])
            self.assertEqual(row['unresolved_fields'], [])
            self.assertEqual(result['trace']['state'], 'preserved')
            self.assertEqual(result['restart_acceptance'], 'not_assessed')
            for action in ('compare', 'run', 'compare'):
                result = self.execute(action, r)
                self.assertEqual(result['result'], 'passed')
                self.assertEqual(result['mount_order_equivalence_verified'], ['lf-clickhouse'])
                self.assertFalse(result['restart_requested_this_invocation'])
            self.assertEqual(len(d.mutations), 1)
            self.assertEqual(baseline, r.baseline_path.read_bytes())
            self.assertEqual(trace_before, r.trace_path.read_bytes())
            self.assertTrue(all(method == 'GET' for method, _ in server.requests))

    def test_order_proof_never_accepts_changed_mount_properties(self):
        for field, value in [('RW', False), ('Mode', 'ro'), ('Propagation', 'rshared'), ('Source', '/other-data')]:
            with self.subTest(field=field), self.fixture() as (r, d, server):
                self.execute('run', r)
                original = self.legacy_baseline(r)
                d.items['lf-clickhouse']['Mounts'].reverse()
                d.items['lf-clickhouse']['Mounts'][0][field] = value
                with self.assertRaises(ValueError): self.execute('compare', r)
                self.assertEqual(len(d.mutations), 1)
                self.assertEqual(original, r.baseline_path.read_bytes())

    def test_mount_order_does_not_hide_other_host_configuration_changes(self):
        with self.fixture() as (r, d, server):
            self.execute('run', r); original = self.legacy_baseline(r)
            d.items['lf-clickhouse']['Mounts'].reverse()
            d.items['lf-clickhouse']['HostConfig']['Memory'] = 1024
            result = self.execute('diagnose', r)
            row = result['services']['lf-clickhouse']
            self.assertTrue(row['mount_order_equivalence_verified'])
            self.assertEqual(row['unresolved_fields'], ['host_config_sha256'])
            with self.assertRaisesRegex(ValueError, 'lf-clickhouse:host_config_sha256'):
                self.execute('compare', r)
            self.assertEqual(original, r.baseline_path.read_bytes())
            self.assertEqual(len(d.mutations), 1)

    def test_alternating_inspect_array_order_during_capture_and_final_check_is_stable(self):
        with self.fixture() as (r, d, server):
            original_json = d.json
            count = 0
            def unordered(args):
                nonlocal count
                value = original_json(args)
                if args[0] == 'inspect':
                    count += 1
                    if count % 2:
                        for item in value: item['Mounts'].reverse()
                return value
            d.json = unordered
            self.assertEqual(self.execute('run', r)['result'], 'passed')
            self.assertEqual(self.execute('compare', r)['result'], 'passed')
            self.assertEqual(len(d.mutations), 1)

    def test_diagnose_cli_shows_fields_but_no_env_values_and_no_mutations(self):
        with self.fixture() as (r, d, server):
            self.execute('run', r); original = self.legacy_baseline(r)
            d.items['lf-web']['Config']['Env'].append('UNKNOWN_PASSWORD=private-do-not-print')
            stdout = io.StringIO()
            with patch.object(restart, 'Docker', return_value=d), patch.object(manage, 'host_info', return_value=r.settings['host']), \
                 patch.object(restart, 'Client', return_value=server.client), contextlib.redirect_stdout(stdout):
                code = restart.main(['diagnose', '--state-dir', str(r.root)])
            self.assertEqual(code, 0)
            text = stdout.getvalue(); result = json.loads(text)
            self.assertEqual(result['result'], 'diagnosed')
            self.assertEqual(result['services']['lf-web']['unresolved_fields'], ['config_sha256'])
            self.assertFalse(result['baseline_rewritten'])
            self.assertNotIn('private-do-not-print', text)
            self.assertNotIn(r.settings['secret_key'], text)
            self.assertEqual(original, r.baseline_path.read_bytes())
            self.assertEqual(len(d.mutations), 1)

    def test_no_baseline_and_unsupported_mount_lists_fail_without_writes(self):
        with self.fixture() as (r, d, server):
            with self.assertRaises(FileNotFoundError): self.execute('diagnose', r)
            self.assertFalse(r.baseline_path.exists()); self.assertFalse(d.mutations)
            mounts = copy.deepcopy(d.items['lf-clickhouse']['Mounts'])
            for bad in (mounts + [mounts[0]], [mounts[0], mounts[0]]):
                with self.assertRaises(ValueError): restart.mount_order_hashes(bad)
            self.assertEqual(restart.mount_order_hashes([]), [common.fingerprint([])])


if __name__ == '__main__': unittest.main()
