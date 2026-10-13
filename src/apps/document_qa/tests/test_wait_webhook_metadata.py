"""Regressions for n8n-generated Wait metadata; no production API or GPU."""
import copy
import unittest
from unittest.mock import patch
from uuid import uuid4

from src.apps.document_qa.contracts import fingerprint
from src.apps.document_qa.intake import UploadError
from src.apps.document_qa.workflow_template import build as entry_build, matches
from src.apps.document_qa.refinement_flow_template import build
from src.apps.document_qa.tests.test_refinement_flow import Fixture
from src.apps.document_qa.tests.test_workflows import FakeEngine


def wait_node(flow):
    return next(node for node in flow['nodes'] if node['name'] == 'Wait before observation')


def legacy_matches(actual, wanted):
    # Reproduce the pre-fix rejection after the server has created the flow.
    if any(node.get('type') == 'n8n-nodes-base.wait' and 'webhookId' in node
           for node in actual.get('nodes', [])):
        return False
    return matches(actual, wanted)


class MetadataEngine(FakeEngine):
    def request(self, method, route, body=None, headers=None):
        result = super().request(method, route, body, headers)
        if method == 'POST' and route == '/api/v1/workflows':
            saved = self.resources['workflows'][result['id']]
            for node in saved['nodes']:
                if node['type'] == 'n8n-nodes-base.wait':
                    node['webhookId'] = str(uuid4())
        if method == 'POST' and route.endswith('/publish'):
            saved = self.resources['workflows'][route.split('/')[4]]
            saved['activeVersion'] = copy.deepcopy({
                key: saved[key] for key in ('nodes', 'connections', 'settings')
            })
        return result


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.wanted, self.digest = build(
            'http://127.0.0.1:17110', 'document-qa-test', 'qa_credential', 'hook_credential'
        )
        self.actual = copy.deepcopy(self.wanted)
        wait_node(self.actual)['webhookId'] = str(uuid4())

    def test_time_interval_wait_accepts_generated_uuid_without_mutating_inputs(self):
        before = copy.deepcopy((self.actual, self.wanted))
        self.assertTrue(matches(self.actual, self.wanted))
        self.assertEqual((self.actual, self.wanted), before)
        self.assertNotIn('webhookId', wait_node(self.wanted))
        self.assertTrue(matches(self.wanted, self.wanted))

    def test_invalid_or_noncanonical_generated_identifiers_are_rejected(self):
        for value in (None, True, 7, {}, [], '', 'not-an-id', 'https://example.invalid',
                      '00000000-0000-0000-0000-000000000000', str(uuid4()).upper(),
                      uuid4().hex, '{' + str(uuid4()) + '}'):
            with self.subTest(value=value):
                actual = copy.deepcopy(self.actual)
                wait_node(actual)['webhookId'] = value
                self.assertFalse(matches(actual, self.wanted))

    def test_wait_mode_duration_version_and_unknown_fields_still_compare(self):
        for field, value in (('resume', 'webhook'), ('resume', 'form'),
                             ('resume', 'specificTime'), ('amount', 6), ('unit', 'minutes'),
                             ('options', {'webhookSuffix': 'other'})):
            with self.subTest(field=field, value=value):
                actual = copy.deepcopy(self.actual)
                wait_node(actual)['parameters'][field] = value
                self.assertFalse(matches(actual, self.wanted))
        for field, value in (('typeVersion', 1), ('type', 'n8n-nodes-base.webhook'),
                             ('retryOnFail', True), ('disabled', True), ('otherMetadata', 'extra')):
            with self.subTest(field=field):
                actual = copy.deepcopy(self.actual)
                wait_node(actual)[field] = value
                self.assertFalse(matches(actual, self.wanted))

    def test_generated_uuid_is_not_ignored_on_other_nodes_or_wait_modes(self):
        for index, node in enumerate(self.wanted['nodes']):
            if node['type'] == 'n8n-nodes-base.wait':
                continue
            with self.subTest(node=node['name']):
                actual = copy.deepcopy(self.wanted)
                actual['nodes'][index]['webhookId'] = str(uuid4())
                self.assertFalse(matches(actual, self.wanted))
        for mode in ('webhook', 'form', 'specificTime'):
            wanted = copy.deepcopy(self.wanted)
            wait_node(wanted)['parameters']['resume'] = mode
            actual = copy.deepcopy(wanted)
            wait_node(actual)['webhookId'] = str(uuid4())
            self.assertFalse(matches(actual, wanted))

    def test_explicit_wait_and_entry_webhook_identifiers_remain_exact(self):
        wanted = copy.deepcopy(self.wanted)
        wait_node(wanted)['webhookId'] = str(uuid4())
        self.assertTrue(matches(wanted, wanted))
        self.assertFalse(matches(self.actual, wanted))
        entry, _ = entry_build('http://127.0.0.1:17110', 'document-qa-test', 'qa', 'hook')
        self.assertTrue(matches(entry, entry))
        actual = copy.deepcopy(entry)
        actual['nodes'][0]['webhookId'] = str(uuid4())
        self.assertFalse(matches(actual, entry))

    def test_call_targets_credentials_code_connections_and_policy_remain_exact(self):
        for mutate in (
            lambda f: f['nodes'][6]['parameters'].update(url='https://example.invalid'),
            lambda f: f['nodes'][6]['parameters'].update(authentication='none'),
            lambda f: f['nodes'][6]['credentials']['httpHeaderAuth'].update(id='other'),
            lambda f: f['nodes'][1]['parameters'].update(jsCode='return [];'),
            lambda f: f.update(connections={}),
            lambda f: f['settings'].update(executionTimeout=601),
            lambda f: f['settings'].update(callerPolicy='any'),
        ):
            actual = copy.deepcopy(self.actual)
            mutate(actual)
            self.assertFalse(matches(actual, self.wanted))


class RegistrationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.engine = MetadataEngine(self.flow)
        self.engine.resources = self.entry_engine.resources

    def partial_registration(self):
        self.entry.prepare('http://127.0.0.1:17110', self.key, self.entry_engine)
        with patch('src.apps.document_qa.workflows.matches', side_effect=legacy_matches):
            with self.assertRaises(UploadError) as error:
                self.flow.prepare('http://127.0.0.1:17110', self.key, self.engine)
            self.assertEqual(error.exception.code, 'ENGINE_WORKFLOW_CHANGED')
        setup = self.flow.setup()
        self.assertFalse(setup['prepared'])
        self.assertIn('id', setup['resources']['workflow'])
        return setup

    def test_partial_registration_reuses_all_ids_keys_body_digest_and_entry(self):
        partial = self.partial_registration()
        entry_before = copy.deepcopy(self.entry.setup())
        entry_id = entry_before['resources']['workflow']['id']
        entry_resource = copy.deepcopy(self.engine.resources['workflows'][entry_id])
        keys_before = {p.name: p.read_bytes() for p in self.root.iterdir()
                       if 'token' in p.name or 'secret' in p.name}
        library_before = self.uploads.library()
        wanted_before, digest_before = self.flow.desired(partial)
        self.assertEqual(fingerprint(wanted_before), partial['resources']['workflow']['request_sha256'])
        start = len(self.engine.calls)
        result = self.flow.prepare('http://127.0.0.1:17110', self.key, self.engine)
        self.assertTrue(result['workflow_configured'])
        fresh_posts = [call for call in self.engine.calls[start:] if call[0] == 'POST']
        self.assertEqual([call[1] for call in fresh_posts], [
            '/api/v1/workflows/' + partial['resources']['workflow']['id'] + '/publish'
        ])
        prepared = self.flow.setup()
        self.assertEqual(prepared['resources'], partial['resources'])
        self.assertEqual(prepared['path'], partial['path'])
        self.assertEqual(self.flow.desired(prepared), (wanted_before, digest_before))
        self.assertEqual(prepared['definition_sha256'], digest_before)
        self.assertEqual(self.entry.setup(), entry_before)
        self.assertEqual(self.engine.resources['workflows'][entry_id], entry_resource)
        self.assertEqual(self.uploads.library(), library_before)
        self.assertEqual(keys_before, {p.name: p.read_bytes() for p in self.root.iterdir()
                                      if p.name in keys_before})
        start = len(self.engine.calls)
        self.flow.prepare('http://127.0.0.1:17110', self.key, self.engine)
        self.assertFalse(any(call[0] == 'POST' for call in self.engine.calls[start:]))
        self.assertFalse(result['question_ready'])

    def test_changed_partial_definition_blocks_publication_without_overwrite(self):
        partial = self.partial_registration()
        actual = self.engine.resources['workflows'][partial['resources']['workflow']['id']]
        wait_node(actual)['parameters']['amount'] = 6
        start = len(self.engine.calls)
        with self.assertRaises(UploadError) as error:
            self.flow.prepare('http://127.0.0.1:17110', self.key, self.engine)
        self.assertEqual(error.exception.code, 'ENGINE_WORKFLOW_CHANGED')
        self.assertFalse(any(call[0] == 'POST' for call in self.engine.calls[start:]))
        self.assertEqual(self.flow.setup(), partial)
        self.assertFalse(actual['active'])

    def test_published_history_is_checked_and_changed_history_blocks_handoff(self):
        self.entry.prepare('http://127.0.0.1:17110', self.key, self.entry_engine)
        self.flow.prepare('http://127.0.0.1:17110', self.key, self.engine)
        setup = self.flow.setup()
        actual = self.engine.resources['workflows'][setup['resources']['workflow']['id']]
        self.flow.verify_setup(setup, self.engine)
        self.assertTrue(matches(actual, self.flow.desired(setup)[0]))
        actual['activeVersion']['nodes'][6]['parameters']['url'] = 'https://example.invalid'
        start = len(self.engine.calls)
        with self.assertRaises(UploadError) as error:
            self.flow.verify_setup(setup, self.engine)
        self.assertEqual(error.exception.code, 'ENGINE_WORKFLOW_CHANGED')
        self.assertFalse(any(call[0] == 'POST' for call in self.engine.calls[start:]))


if __name__ == '__main__':
    unittest.main()
