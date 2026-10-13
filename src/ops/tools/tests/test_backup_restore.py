"""Pinned private-copy boundary; no source execution or Docker connection."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from src.ops.tools import check_backup_restore as check


class Boundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name); self.root = self.folder/'project'; self.root.mkdir(mode=0o700)
        self.target = self.folder/'checked'
        self.spec = json.loads((check.ROOT/check.REL/'manifest.json').read_text())
        rows = self.spec['base_files'] + self.spec['auxiliary_files']
        for row in rows:
            path = self.root/row['path']; path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            path.write_bytes((check.ROOT/row['path']).read_bytes())
        self.manifest = self.root/check.REL/'manifest.json'; self.manifest.write_text(json.dumps(self.spec))

    def test_private_copy_exact_pins_and_active_hash(self):
        result = check.prepare(self.root,self.target)
        self.assertEqual(result['active_code_sha256'],self.spec['active_code_sha256'])
        self.assertEqual(result['active_code_files'],61)
        self.assertTrue(all((p.stat().st_mode & 0o077)==0 for p in self.target.rglob('*')))

    def test_changed_candidate_refused_before_any_copy(self):
        (self.root/check.REL/'restore.py').write_text('changed')
        with self.assertRaises(check.check.__globals__['Invalid']): check.prepare(self.root,self.target)
        self.assertFalse(self.target.exists())

    def test_unreviewed_active_module_refused_before_any_copy(self):
        (self.root/'src/ops/platform/extra.py').write_text('extra')
        with self.assertRaises(Exception): check.prepare(self.root,self.target)
        self.assertFalse(self.target.exists())

    def test_manifest_auxiliary_scope_not_adopted(self):
        self.spec['auxiliary_files'].append({'path':'src/ops/tools/unreviewed.py','sha256':'0'*64})
        self.manifest.write_text(json.dumps(self.spec))
        with self.assertRaises(Exception): check.prepare(self.root,self.target)
        self.assertFalse(self.target.exists())
