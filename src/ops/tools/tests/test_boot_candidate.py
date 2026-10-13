"""Staging boundary regression: source integrity and refusal before copy."""
from pathlib import Path
import json
import shutil
import tempfile
import unittest

from src.ops.tools import check_boot_candidate as check


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ops-candidate-boundary-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'root';self.root.mkdir(mode=0o700)
        self.spec=json.loads((check.ROOT/check.RELATIVE/'manifest.json').read_text())
        for row in self.spec['base_files']:
            path=self.root/row['path'];path.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(check.ROOT/row['path'],path)
        shutil.copytree(check.ROOT/check.RELATIVE,self.root/check.RELATIVE)
        self.destination=Path(self.temp.name)/'copy'

    def test_copy_keeps_active_source_and_builds_expected_candidate(self):
        before={str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result=check.prepare(self.root,self.destination)
        self.assertEqual(result['active_code_files'],60);self.assertEqual(result['candidate_code_files'],61)
        after={str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before,after)
        source={p:check.safe_read(self.destination,p) for p in check.code_set(self.destination)}
        self.assertEqual(check.code_hash(source),self.spec['candidate_code_sha256'])

    def test_changed_active_code_refused_without_temporary_copy(self):
        (self.root/'src/ops/platform/ops/refresh_worker.py').write_text('# manual change')
        with self.assertRaisesRegex(check.Invalid,'CANDIDATE_BASE_CHANGED'):check.prepare(self.root,self.destination)
        self.assertFalse(self.destination.exists())

    def test_added_runtime_dependency_refused(self):
        (self.root/'src/ops/platform/ops/extra.py').write_text('# changed code set')
        with self.assertRaisesRegex(check.Invalid,'CANDIDATE_SOURCE_SET_CHANGED'):check.prepare(self.root,self.destination)
        self.assertFalse(self.destination.exists())

    def test_modified_candidate_payload_refused(self):
        (self.root/check.RELATIVE/'source/src/ops/platform/ops/boot_lifecycle.py').write_text('# modified')
        with self.assertRaisesRegex(check.Invalid,'CANDIDATE_PAYLOAD_CHANGED'):check.prepare(self.root,self.destination)
        self.assertFalse(self.destination.exists())

    def test_symlink_payload_and_source_refused(self):
        for path in (self.root/check.RELATIVE/'source/src/ops/platform/ops/boot_lifecycle.py',
                     self.root/'src/ops/platform/ops/worker_state.py'):
            original=path.read_bytes();path.unlink();path.symlink_to(check.ROOT/'src/ops/platform/ops/worker_state.py')
            with self.assertRaisesRegex(check.Invalid,'CANDIDATE_SYMLINK_REFUSED'):check.prepare(self.root,self.destination)
            path.unlink();path.write_bytes(original)
        self.assertFalse(self.destination.exists())

    def test_manifest_cannot_add_private_state_as_source(self):
        self.spec['base_files'].append({'path':'src/platform/agent/.secrets/api-token','sha256':'a'*64})
        (self.root/check.RELATIVE/'manifest.json').write_text(json.dumps(self.spec))
        with self.assertRaisesRegex(check.Invalid,'CANDIDATE_SOURCE_SET_CHANGED'):check.prepare(self.root,self.destination)
        self.assertFalse(self.destination.exists())


if __name__=='__main__':unittest.main()
