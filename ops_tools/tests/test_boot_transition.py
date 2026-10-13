"""Only pinned sources enter the isolated stopped-transition checker."""
from pathlib import Path
import json
import shutil
import tempfile
import unittest

from ops_tools import check_boot_transition as check


class CopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='transition-copy-test-'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'; self.root.mkdir(mode=0o700)
        self.spec = json.loads((check.ROOT / check.RELATIVE / 'manifest.json').read_text())
        for row in self.spec['base_files'] + self.spec['fixtures']:
            path = self.root / row['path']; path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(check.ROOT / row['path'], path)
        shutil.copytree(check.ROOT / check.RELATIVE, self.root / check.RELATIVE)
        self.copy = Path(self.temp.name) / 'copy'

    def test_copy_contains_original_runtime_and_separate_candidate(self):
        result = check.prepare(self.root, self.copy)
        self.assertEqual(result['active_code_files'], 60); self.assertEqual(result['candidate_code_files'], 61)
        files = {p: check.safe_read(self.copy, p) for p in check.code_set(self.copy)}
        self.assertEqual(check.code_hash(files), self.spec['base_code_sha256'])
        self.assertTrue((self.copy / check.RELATIVE / 'transition.py').is_file())

    def test_modified_base_or_added_runtime_refused_before_copy(self):
        path = self.root / 'ops_platform/ops/refresh_worker.py'; raw = path.read_bytes(); path.write_text('# changed')
        with self.assertRaisesRegex(check.Invalid, 'TRANSITION_BASE_CHANGED'): check.prepare(self.root, self.copy)
        path.write_bytes(raw); (self.root / 'ops_platform/extra.py').write_text('# extra')
        with self.assertRaisesRegex(check.Invalid, 'TRANSITION_SOURCE_SET_CHANGED'): check.prepare(self.root, self.copy)
        self.assertFalse(self.copy.exists())

    def test_modified_core_or_tests_refused_before_execution(self):
        for name in ('transition.py', 'tests/test_transition.py'):
            path = self.root / check.RELATIVE / name; raw = path.read_bytes(); path.write_text('# changed')
            with self.assertRaisesRegex(check.Invalid, 'TRANSITION_AUXILIARY_CHANGED'): check.prepare(self.root, self.copy)
            path.write_bytes(raw)
        self.assertFalse(self.copy.exists())

    def test_modified_candidate_or_profile_refused(self):
        path = self.root / check.RELATIVE / 'source/ops_platform/ops/worker_state.py'; raw = path.read_bytes(); path.write_text('# changed')
        with self.assertRaisesRegex(check.Invalid, 'TRANSITION_PAYLOAD_CHANGED'): check.prepare(self.root, self.copy)
        path.write_bytes(raw); (self.root / 'ops_platform/profiles/docker-compose.json').write_text('{}')
        with self.assertRaisesRegex(check.Invalid, 'TRANSITION_BASE_CHANGED'): check.prepare(self.root, self.copy)
        self.assertFalse(self.copy.exists())

    def test_symlink_payload_refused(self):
        path = self.root / check.RELATIVE / 'transition.py'; path.unlink(); path.symlink_to(check.ROOT / check.RELATIVE / 'transition.py')
        with self.assertRaisesRegex(check.Invalid, 'CANDIDATE_SYMLINK_REFUSED'): check.prepare(self.root, self.copy)
        self.assertFalse(self.copy.exists())

    def test_manifest_cannot_include_credentials_or_external_targets(self):
        self.spec['fixtures'].append({'path': 'agent_platform/.secrets/api-token', 'sha256': 'a' * 64})
        (self.root / check.RELATIVE / 'manifest.json').write_text(json.dumps(self.spec))
        with self.assertRaisesRegex(check.Invalid, 'TRANSITION_SCOPE_INVALID'): check.prepare(self.root, self.copy)
        self.assertFalse(self.copy.exists())


if __name__ == '__main__': unittest.main()
