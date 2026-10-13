"""Version fallback evidence and entry/CPU registration; no real Docker or models."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from src.apps.document_qa.engine_identity import (ENGINE_VERSION, IMAGE_DIGEST, IMAGE_REF,
                                             REPO_DIGEST, local_image_identity, verify_engine_version)
from src.apps.document_qa.intake import UploadError
from src.apps.document_qa.workflows import WorkflowStore
from src.apps.document_qa.uploads import UploadStore
from src.apps.document_qa.refinement import RefinementStore
from src.apps.document_qa.refinement_flow import RefinementFlow
from src.apps.document_qa.tests.test_workflows import FakeEngine, DataFixture

ORIGIN = 'http://127.0.0.1:16200'


class DockerFixture:
    def __init__(self):
        self.calls = []
        self.endpoint = 'unix:///var/run/docker.sock'
        self.ids = ['a' * 64]
        self.container = {'running': True, 'project': 'infrastructure', 'service': 'n8n',
                          'image_ref': IMAGE_REF, 'image_id': 'sha256:' + 'b' * 64,
                          'ports': {'5678/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '16200'}]}}
        self.image = {'id': 'sha256:' + 'b' * 64, 'repo_digests': [REPO_DIGEST]}

    def __call__(self, args):
        self.calls.append(args)
        if args[1:3] == ['context', 'inspect']:
            return json.dumps(self.endpoint)
        if args[1] == 'ps':
            return '\n'.join(self.ids)
        if args[1] == 'inspect':
            return json.dumps(self.container)
        if args[1:3] == ['image', 'inspect']:
            return json.dumps(self.image)
        raise AssertionError(args)

    def read(self, origin):
        return local_image_identity(origin, self, {})


class Settings:
    origin = ORIGIN
    def __init__(self, data):
        self.data, self.calls = data, []
    def request(self, method, route):
        self.calls.append((method, route))
        return {'data': self.data}


class MissingVersionEngine(FakeEngine):
    def __init__(self, workflow, reader):
        super().__init__(workflow)
        self.identity_reader = reader
    def request(self, method, route, body=None, headers=None):
        if route == '/rest/settings':
            self.calls.append((method, route, copy.deepcopy(body), headers))
            return {'data': {'public_settings': True}}
        return super().request(method, route, body, headers)


class IdentityTests(unittest.TestCase):
    def test_missing_version_matches_owned_running_image_and_published_API(self):
        docker = DockerFixture()
        proof = verify_engine_version(Settings({}), docker.read)
        self.assertEqual(proof, {'version': ENGINE_VERSION, 'origin': ORIGIN,
                                 'source': 'owned_local_pinned_image', 'image_digest': IMAGE_DIGEST})
        self.assertEqual(len(docker.calls), 4)
        self.assertTrue(all(not set(a).intersection({'exec','run','start','stop','restart','up','pull','build'}) for a in docker.calls))
        self.assertTrue(all('.Config.Env' not in ' '.join(a) for a in docker.calls))

    def test_existing_matching_HTTP_version_needs_no_deployment_reader(self):
        def refused(_):
            raise AssertionError('unexpected Docker dependency')
        self.assertEqual(verify_engine_version(Settings({'versionCli': ENGINE_VERSION}), refused)['source'],
                         'settings_versionCli')

    def test_reported_mismatched_or_invalid_version_cannot_fallback(self):
        for version in ('2.40.6', None, False, 'unverified-version'):
            with self.subTest(version=version):
                docker = DockerFixture()
                with self.assertRaises(UploadError) as error:
                    verify_engine_version(Settings({'versionCli': version}), docker.read)
                self.assertEqual(error.exception.code, 'ENGINE_VERSION_UNVERIFIED')
                self.assertEqual(docker.calls, [])

    def test_settings_shape_is_required_even_with_a_pinned_image(self):
        docker = DockerFixture()
        with self.assertRaises(UploadError):
            verify_engine_version(Settings([]), docker.read)
        self.assertEqual(docker.calls, [])

    def test_remote_API_and_remote_Docker_are_not_local_evidence(self):
        for origin in ('https://example.test', 'http://192.168.0.2:16200'):
            docker = DockerFixture()
            with self.assertRaises(UploadError):
                docker.read(origin)
            self.assertEqual(docker.calls, [])
        docker = DockerFixture(); docker.endpoint = 'tcp://remote:2376'
        with self.assertRaises(UploadError):
            docker.read(ORIGIN)
        self.assertEqual(len(docker.calls), 1)

    def test_ambiguous_container_or_owner_or_stopped_container_is_refused(self):
        for change in ('count', 'owner', 'stopped'):
            docker = DockerFixture()
            if change == 'count': docker.ids.append('c' * 64)
            if change == 'owner': docker.container['project'] = 'unrelated'
            if change == 'stopped': docker.container['running'] = False
            with self.subTest(change=change), self.assertRaises(UploadError):
                docker.read(ORIGIN)

    def test_wrong_image_reference_or_digest_or_image_id_is_refused(self):
        for change in ('reference', 'repo_digest', 'image_id'):
            docker = DockerFixture()
            if change == 'reference': docker.container['image_ref'] = 'n8nio/n8n:other'
            if change == 'repo_digest': docker.image['repo_digests'] = ['n8nio/n8n@sha256:' + 'd' * 64]
            if change == 'image_id': docker.image['id'] = 'sha256:' + 'd' * 64
            with self.subTest(change=change), self.assertRaises(UploadError):
                docker.read(ORIGIN)

    def test_known_version_tag_still_requires_verified_content_digest(self):
        docker = DockerFixture(); docker.container['image_ref'] = 'n8nio/n8n:2.40.5'
        self.assertEqual(docker.read(ORIGIN)['image_digest'], IMAGE_DIGEST)
        docker.image['repo_digests'] = []
        with self.assertRaises(UploadError): docker.read(ORIGIN)

    def test_another_published_port_or_nonlocal_bind_is_refused(self):
        for binding in ({'HostIp':'0.0.0.0','HostPort':'16201'}, {'HostIp':'192.168.0.2','HostPort':'16200'}):
            docker = DockerFixture(); docker.container['ports']['5678/tcp'] = [binding]
            with self.subTest(binding=binding), self.assertRaises(UploadError):
                docker.read(ORIGIN)

    def test_ambiguous_endpoint_override_is_refused_before_commands(self):
        docker = DockerFixture()
        with self.assertRaises(UploadError):
            local_image_identity(ORIGIN, docker, {'DOCKER_HOST':'unix:///var/run/docker.sock','DOCKER_CONTEXT':'other'})
        self.assertEqual(docker.calls, [])

    def test_raw_metadata_failure_is_not_exposed(self):
        def failed(_): raise ValueError('PRIVATE_SENTINEL')
        with self.assertRaises(UploadError) as error:
            local_image_identity(ORIGIN, failed, {})
        self.assertEqual(error.exception.code, 'ENGINE_VERSION_UNVERIFIED')
        self.assertNotIn('PRIVATE_SENTINEL', str(error.exception))

    def test_evidence_for_another_API_origin_is_refused(self):
        proof = DockerFixture().read(ORIGIN); proof['origin'] = 'http://127.0.0.1:16201'
        with self.assertRaises(UploadError):
            verify_engine_version(Settings({}), lambda _: proof)


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qa-version-tests-')
        self.root = Path(self.temp.name)
        self.uploads = UploadStore.initialize(self.root/'uploads')
        self.entry = WorkflowStore.initialize(self.uploads)
        RefinementStore.initialize(self.uploads)
        self.flow = RefinementFlow.initialize(self.uploads)
        self.docker = DockerFixture()
        self.engine = MissingVersionEngine(self.entry, self.docker.read)
        self.key = self.root/'engine-key.json'
        self.key.write_text(json.dumps({'url':ORIGIN,'api_key':'fixture-only-key'})); self.key.chmod(0o600)
        self.qa = 'http://127.0.0.1:17110'

    def tearDown(self):
        self.temp.cleanup()

    def test_entry_and_CPU_register_without_version_field_and_reuse_same_resources(self):
        identity = self.uploads.library()['library_id']; token = self.uploads.token_path.read_bytes()
        self.entry.prepare(self.qa, self.key, self.engine)
        cpu = MissingVersionEngine(self.flow, self.docker.read); cpu.resources = self.engine.resources
        self.flow.prepare(self.qa, self.key, cpu)
        before = copy.deepcopy(cpu.resources)
        self.entry.prepare(self.qa, self.key, self.engine); self.flow.prepare(self.qa, self.key, cpu)
        self.assertEqual(before, cpu.resources)
        self.assertEqual(len(cpu.resources['workflows']), 2)
        self.assertEqual(len(cpu.resources['credentials']), 4)
        self.assertEqual(self.uploads.library()['library_id'], identity)
        self.assertEqual(self.uploads.token_path.read_bytes(), token)
        self.assertTrue(self.entry.status()['workflow_configured'])
        self.assertTrue(self.flow.status()['workflow_configured'])
        self.assertFalse(self.flow.status()['question_ready'])

    def test_failed_image_proof_creates_no_remote_resources(self):
        self.docker.image['repo_digests'] = []
        with self.assertRaises(UploadError): self.entry.prepare(self.qa, self.key, self.engine)
        self.assertFalse(any(c[0] == 'POST' for c in self.engine.calls))
        self.assertFalse(self.entry.status()['workflow_configured'])

    def test_image_change_after_prepare_blocks_delivery_and_preserves_intent(self):
        self.entry.prepare(self.qa, self.key, self.engine)
        job = self.uploads.accept(str(uuid4()), 'fixture', 'x.txt', b'text', DataFixture.origin)
        job = self.uploads.dispatch(job['job_id'], DataFixture())
        self.docker.container['image_ref'] = 'n8nio/n8n:other'
        with self.assertRaises(UploadError): self.entry.dispatch(job['job_id'], self.engine)
        self.assertEqual(self.entry.handoff(job['job_id'])['attempts'], 0)
        self.assertFalse(any(c[1].startswith('/webhook/') for c in self.engine.calls))
        self.assertEqual(self.uploads.job(job['job_id'])['source_document_id'], job['source_document_id'])


if __name__ == '__main__': unittest.main()
