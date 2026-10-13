"""Copy boundaries; use miniature source trees and no production inspection."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.ops.tools import check_maintenance as checker


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'; self.root.mkdir(mode=0o700)
        self.target = Path(self.temp.name) / 'copy'
        self.runtime = ['src/ops/platform/' + str(n) + '.py' for n in range(61)]
        rows = self.runtime + sorted(checker.FIXTURES | checker.AUXILIARY)
        for name in rows:
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('# fixture\n')
        self.spec = {'schema': 1, 'activation_supported': False, 'production_cli_available': False,
            'tests_run': checker.CASES, 'active_code_files': 61,
            'active_code_sha256': checker.code_hash({p: (self.root / p).read_bytes() for p in self.runtime}),
            'base_files': [{'path': p, 'sha256': checker.sha((self.root / p).read_bytes())} for p in self.runtime + sorted(checker.FIXTURES)],
            'auxiliary_files': [{'path': p, 'sha256': checker.sha((self.root / p).read_bytes())} for p in sorted(checker.AUXILIARY)]}
        self.save()

    def save(self): (self.root / checker.RELATIVE / 'manifest.json').write_text(json.dumps(self.spec))
    def copied(self):
        with patch.object(checker, 'code_set', return_value=self.runtime): return checker.prepare(self.root, self.target)

    def test_reviewed_scope_copies_current_runtime_without_overlay(self):
        result = self.copied(); self.assertEqual(result['active_code_files'], 61)
        for p in self.runtime: self.assertEqual((self.root / p).read_bytes(), (self.target / p).read_bytes())

    def test_changed_auxiliary_is_rejected_before_any_copy(self):
        (self.root / checker.RELATIVE / 'maintenance.py').write_text('changed')
        with self.assertRaisesRegex(checker.Invalid, 'MAINTENANCE_SOURCE_CHANGED'): self.copied()
        self.assertFalse(self.target.exists())

    def test_extra_runtime_or_manifest_scope_is_rejected_before_any_copy(self):
        self.runtime.append('src/ops/platform/extra.py')
        with self.assertRaisesRegex(checker.Invalid, 'MAINTENANCE_SOURCE_SET_CHANGED'): self.copied()
        self.assertFalse(self.target.exists())

    def test_source_symlink_is_rejected_before_any_copy(self):
        path = self.root / checker.RELATIVE / 'maintenance.py'; path.unlink(); path.symlink_to(self.root / self.runtime[0])
        with self.assertRaisesRegex(checker.Invalid, 'CANDIDATE_SYMLINK_REFUSED'): self.copied()
        self.assertFalse(self.target.exists())

    def test_candidate_cannot_claim_production_activation(self):
        self.spec['activation_supported'] = True; self.save()
        with self.assertRaisesRegex(checker.Invalid, 'MAINTENANCE_MANIFEST_INVALID'): self.copied()
        self.assertFalse(self.target.exists())
