"""Q02 saved selections through Q05b gates and a resumed fake Knowledge lifecycle."""
import copy
from contextlib import redirect_stdout
import io
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.apps.document_qa import readiness
from src.apps.document_qa.indexing_results import defaults_checked
from src.apps.document_qa.indexing_runtime import IndexingGate
from src.apps.document_qa.intake import UploadError
from src.apps.document_qa.tests import test_indexing
from src.apps.document_qa.tests.test_indexing_flow import Fixture
from src.apps.document_qa.tests.test_readiness import Fake, settings


def configured(path, reader):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(settings()))
    path.chmod(0o600)
    stream = io.StringIO()
    with patch.object(readiness, 'GatewayReader', return_value=reader), redirect_stdout(stream):
        code = readiness.main(['configure', '--settings', str(path),
                               '--connections-from', str(path.parent / 'unused.json')])
    report = json.loads(stream.getvalue())
    assert code == 0 and report['effects']['configuration_written'] is True
    saved, _, _ = readiness.load_settings(path, path.parent / 'unused.json')
    return saved


class SavedDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.reader = Fake()
        self.report = readiness.inspect(settings(), self.reader)
        self.assertTrue(self.report['defaults_ready'])
        self.defaults = self.report['defaults']
        self.core = readiness.embedding_profile(self.reader.f['embedding'], self.reader.f['profile'])

    def gate(self, value=None):
        current = settings()
        current['defaults'] = value or self.defaults
        return IndexingGate(current, True, lambda _: self.reader)

    def test_all_optional_display_field_combinations_project_to_same_immutable_selection(self):
        before = copy.deepcopy(self.defaults)
        for count in range(4):
            for keys in itertools.combinations(('name', 'version', 'label'), count):
                with self.subTest(keys=keys):
                    value = copy.deepcopy(self.defaults)
                    value['embedding'] = {**self.core, **{k: self.defaults['embedding'][k] for k in keys}}
                    result = defaults_checked(value)
                    self.assertEqual(result, {'embedding': self.core, 'chunker': self.defaults['chunker']})
                    result['chunker']['options']['max_characters'] += 1
                    self.assertEqual(value['chunker']['options'], self.defaults['chunker']['options'])
        self.assertEqual(self.defaults, before)

    def test_changed_display_labels_keep_same_binding_and_fresh_profile_comparison(self):
        value = copy.deepcopy(self.defaults)
        value['embedding'].update(name='표시 이름 변경', version='표시 버전 변경', label='변경된 이름')
        self.assertEqual(self.gate(value)(20), self.gate()(20))

    def test_missing_unknown_and_non_string_display_fields_remain_invalid_before_native_GET(self):
        cases = []
        for key in self.core:
            value = copy.deepcopy(self.defaults)
            del value['embedding'][key]
            cases.append(value)
        for key, field in (('dimension', 1024), ('allow_inference', True),
                           ('name', None), ('version', {}), ('label', 123)):
            value = copy.deepcopy(self.defaults)
            value['embedding'][key] = field
            cases.append(value)
        for value in cases:
            with self.subTest(profile=value['embedding']):
                calls = self.reader.calls
                with self.assertRaisesRegex(UploadError, 'INDEXING_PROFILE_INVALID'):
                    self.gate(value)(20)
                self.assertEqual(self.reader.calls, calls)

    def test_core_identity_profile_and_token_limits_remain_strict(self):
        for key, value in (('deployment_id', 'invalid'), ('model_version_id', None),
                           ('embedding_space_id', 'es_bad'), ('profile_id', 'other-profile'),
                           ('max_input_tokens', True), ('max_input_tokens', 127),
                           ('max_input_tokens', 8193), ('max_input_bytes', '131072')):
            with self.subTest(field=key, value=value):
                defaults = copy.deepcopy(self.defaults)
                defaults['embedding'][key] = value
                with self.assertRaisesRegex(UploadError, 'INDEXING_PROFILE_INVALID'):
                    defaults_checked(defaults)

    def test_readiness_configure_disk_roundtrip_passes_live_gate_without_rewriting_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'settings.json'
            saved = configured(path, self.reader)
            before = path.read_bytes()
            self.assertEqual(saved['defaults'], self.defaults)
            gate = IndexingGate(saved, True, lambda _: self.reader)
            self.assertEqual(gate(20)['embedding'], self.core)
            self.assertEqual(path.read_bytes(), before)

    def test_paused_server_policy_still_blocks_before_reader_or_defaults(self):
        with self.assertRaisesRegex(UploadError, 'GPU_VERIFICATION_DEFERRED'):
            IndexingGate(settings(), False, lambda _: self.fail('unexpected GET'))(20)

    def test_fresh_embedding_space_mismatch_is_not_hidden_by_display_projection(self):
        self.reader.f['profile']['embedding_space_id'] = 'es_' + 'f' * 64
        with self.assertRaisesRegex(UploadError, 'INDEXING_PROFILE_CHANGED'):
            self.gate()(20)

    def test_fresh_chunker_version_mismatch_remains_blocked(self):
        self.reader.f['chunkers']['items'][1]['version'] = 'future'
        with self.assertRaisesRegex(readiness.ReadinessError, 'CHUNKER_VERSION_CHANGED'):
            self.gate()(20)


class ResumedFlowTests(Fixture):
    def test_configured_metadata_survives_paused_resume_build_and_activation_with_original_receipts(self):
        reader = Fake()
        path = Path(self.temp.name) / 'configuration' / 'settings.json'
        saved = configured(path, reader)
        settings_bytes = path.read_bytes()
        gate = IndexingGate(saved, True, lambda _: reader)
        normalized = defaults_checked(saved['defaults'])
        job = self.canonical_job()
        self.flow.dispatch(job['job_id'], self.engine, paused=True)
        original_grant, original_receipt = self.grant_for(self.flow, job['job_id'])
        self.assertEqual(self.step(original_grant, gate)['error_code'], 'GPU_VERIFICATION_DEFERRED')
        self.assertEqual(self.knowledge.calls, [])
        source_before = self.uploads.job(job['job_id']), self.cpu.job(job['job_id'])
        keys_before = {p.name: p.read_bytes() for p in self.root.iterdir()
                       if 'token' in p.name or 'secret' in p.name}
        self.flow.dispatch(job['job_id'], self.engine, resume=True)
        grant, receipt = self.grant_for(self.flow, job['job_id'])
        self.assertEqual(receipt['generation'], 2)
        self.assertEqual(receipt['inference_mode'], 'server_policy')
        with self.assertRaisesRegex(UploadError, 'INDEXING_GRANT_INVALID'):
            self.flow.grant(original_grant)
        with patch.object(test_indexing, 'DEFAULTS', normalized):
            self.assertEqual(self.step(grant, gate)['state'], 'running')
            self.assertEqual(json.loads(self.indexing.binding()['config']), normalized)
            self.assertEqual(self.step(grant, gate)['indexing']['state'], 'waiting')
            self.mark_ready(job['job_id'])
            self.assertEqual(self.step(grant, gate)['indexing']['state'], 'index_ready')
            result = self.step(grant, gate)
        self.assertEqual(result['state'], 'indexing_activated')
        self.assertTrue(result['indexing']['indexing_active'])
        self.assertFalse(result['question_ready'])
        self.assertEqual(self.indexing.head()['generation'], 1)
        self.assertEqual(len(self.indexing.head()['documents']), 1)
        self.assertEqual(len(self.kb_posts('/builds')), 1)
        self.assertEqual(len(self.kb_posts('/activate')), 1)
        self.assertEqual((self.uploads.job(job['job_id']), self.cpu.job(job['job_id'])), source_before)
        self.assertEqual(keys_before, {name: (self.root / name).read_bytes() for name in keys_before})
        self.assertEqual(path.read_bytes(), settings_bytes)
        with self.uploads.connect() as db:
            old = db.execute('SELECT receipt FROM indexing_flow_runs WHERE handoff_id=?',
                             (original_receipt['handoff_id'],)).fetchone()[0]
        self.assertEqual(json.loads(old), original_receipt)

    def test_invalid_core_with_metadata_stops_before_knowledge_creation(self):
        job, grant, _ = self.started()
        reader = Fake()
        current = settings()
        current['defaults'] = readiness.inspect(current, reader)['defaults']
        current['defaults']['embedding']['max_input_tokens'] = True
        calls = reader.calls
        result = self.step(grant, IndexingGate(current, True, lambda _: reader))
        self.assertEqual(result['state'], 'attention_required')
        self.assertEqual(result['error_code'], 'INDEXING_PROFILE_INVALID')
        self.assertEqual(reader.calls, calls)
        self.assertEqual(self.knowledge.calls, [])
        self.assertIsNone(self.indexing.binding())


if __name__ == '__main__':
    unittest.main()
