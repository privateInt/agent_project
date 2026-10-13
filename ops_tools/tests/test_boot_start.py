"""Pinned candidate/dependency checks before writing temporary runnable code."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from ops_tools import check_boot_start as check, check_boot_transition as transition


class CopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'source'; self.root.mkdir(mode=0o700)
        spec = json.loads((check.ROOT / transition.RELATIVE / 'manifest.json').read_text())
        for row in spec['base_files'] + spec['fixtures']:
            p = self.root / row['path']; p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(check.ROOT / row['path'], p)
        for relative in (check.RELATIVE, transition.RELATIVE):
            shutil.copytree(check.ROOT / relative, self.root / relative)
        self.dest = Path(self.tmp.name) / 'copy'

    def test_temporary_runtime_is_candidate_and_baseline_is_preserved(self):
        result = check.prepare(self.root, self.dest)
        self.assertEqual(result['copied_source_files'], 135)
        for folder, key in ((self.dest, 'candidate_code_sha256'), (self.dest / 'baseline', 'base_code_sha256')):
            self.assertEqual(check.code_hash({p: check.safe_read(folder, p) for p in check.code_set(folder)}), result[key])

    def test_modified_adapter_or_tests_never_reach_copy(self):
        for name in ('arming.py', 'tests/test_arming.py'):
            p = self.root / check.RELATIVE / name; raw = p.read_bytes(); p.write_text('# modified')
            with self.assertRaisesRegex(check.Invalid, 'BOOT_START_AUXILIARY_CHANGED'): check.prepare(self.root, self.dest)
            self.assertFalse(self.dest.exists()); p.write_bytes(raw)

    def test_changed_transition_manifest_or_payload_is_rejected(self):
        p = self.root / transition.RELATIVE / 'manifest.json'; raw = p.read_bytes(); p.write_bytes(raw + b' ')
        with self.assertRaisesRegex(check.Invalid, 'BOOT_START_TRANSITION_CHANGED'): check.prepare(self.root, self.dest)
        p.write_bytes(raw)
        (self.root / transition.RELATIVE / 'transition.py').write_text('# changed')
        with self.assertRaisesRegex(check.Invalid, 'TRANSITION_AUXILIARY_CHANGED'): check.prepare(self.root, self.dest)
        self.assertFalse(self.dest.exists())

    def test_symlink_candidate_is_rejected(self):
        p = self.root / check.RELATIVE / 'arming.py'; p.unlink(); p.symlink_to(check.ROOT / check.RELATIVE / 'arming.py')
        with self.assertRaisesRegex(check.Invalid, 'CANDIDATE_SYMLINK_REFUSED'): check.prepare(self.root, self.dest)
        self.assertFalse(self.dest.exists())

    def test_extra_credentials_or_activation_flag_is_rejected(self):
        p = self.root / check.RELATIVE / 'manifest.json'; original = json.loads(p.read_text())
        value = dict(original, activation_supported=True); p.write_text(json.dumps(value))
        with self.assertRaisesRegex(check.Invalid, 'BOOT_START_MANIFEST_INVALID'): check.prepare(self.root, self.dest)
        value = dict(original, auxiliary_files=original['auxiliary_files'] + [{'path': '../../.secrets/key', 'sha256': 'a'*64}])
        p.write_text(json.dumps(value))
        with self.assertRaisesRegex(check.Invalid, 'BOOT_START_SCOPE_INVALID'): check.prepare(self.root, self.dest)
        self.assertFalse(self.dest.exists())


if __name__ == '__main__': unittest.main()
