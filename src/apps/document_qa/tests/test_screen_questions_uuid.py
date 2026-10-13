"""Server-generated UUIDs cross real disposable QA HTTP into the actual screen JS."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from uuid import UUID, uuid4

from src.apps.document_qa.api import make_server
from src.apps.document_qa.tests.test_question_head import Fixture
from src.apps.document_qa.tests.test_refinement import running

SCRIPT = Path(__file__).with_name('screen_questions_uuid_ui_test.cjs')
NODE_PROBE = """
var c = require('crypto');
console.log(JSON.stringify({version: process.versions.node,
    capable: typeof fetch === 'function' && typeof structuredClone === 'function'
        && typeof FormData === 'function' && typeof AbortController === 'function'
        && typeof TextEncoder === 'function' && typeof Object.hasOwn === 'function'
        && !!c.webcrypto && typeof c.webcrypto.getRandomValues === 'function'}));
"""


def node_runtime():
    """Probe old Node with compatible syntax before loading modern JS fixtures."""
    executable = shutil.which('node')
    requirement = 'HTTP-to-screen JS tests require Node >=18 with fetch, structuredClone and Web Crypto'
    if executable is None:
        return None, requirement + ' (Node not found)'
    try:
        result = subprocess.run([executable, '-e', NODE_PROBE], capture_output=True,
                                text=True, timeout=5)
        report = json.loads(result.stdout) if result.returncode == 0 else None
        version = report.get('version') if isinstance(report, dict) else None
        parts = version.split('.') if isinstance(version, str) else []
        valid = len(parts) == 3 and all(p.isdecimal() for p in parts)
        if valid and int(parts[0]) >= 18 and report.get('capable') is True:
            return executable, ''
        detail = 'found Node ' + version if valid else 'runtime probe failed'
        return None, requirement + ' (' + detail + ')'
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return None, requirement + ' (runtime probe unavailable)'


class FinishedAgent:
    def __init__(self, fake):
        self.fake = fake

    def __getattr__(self, name):
        return getattr(self.fake, name)

    def get(self, path):
        reply = self.fake.get(path)
        if path.startswith('/api/agent-runs/') and reply['mode'] == 'run' and reply['state'] == 'accepted':
            self.fake.finish()  # The copied first response stays accepted; later observe reads completion.
        return reply


class QuestionUUIDScreenTests(Fixture):
    @classmethod
    def setUpClass(cls):
        cls.node, reason = node_runtime()
        if cls.node is None:
            raise unittest.SkipTest(reason)

    def screen(self, mode, port=None, **extra):
        payload = {'mode': mode, **extra}
        if port is not None:
            payload.update(origin='http://127.0.0.1:' + str(port),
                           token=self.uploads.token_path.read_text().strip())
        result = subprocess.run([self.node, str(SCRIPT)],
                                input=json.dumps(payload), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['result'], 'passed')
        return report

    def server(self, inference=True):
        return make_server(self.uploads, object(), port=0, qa_agent=self.qa,
                           question_head=self.head, agent_factory=lambda: FinishedAgent(self.fake),
                           question_inference_enabled=inference)

    def preserved(self):
        return (self.head.status()['head_revision'], self.head.status()['head_target_id'],
                self.uploads.token_path.read_bytes(), self.uploads.jobs(), self.uploads.library())

    def test_canonical_server_UUID_validation_and_browser_key_contract(self):
        before = self.snapshot()
        self.screen('validate')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.fake.posts('/api/agent-runs'), [])

    def test_real_v5_target_and_question_history_load_without_inference(self):
        receipt = self.active()
        self.assertEqual(UUID(receipt['target_id']).version, 5)
        q = self.head.accept(str(uuid4()), '기존 질문 기록')
        self.assertEqual(UUID(q['question_id']).version, 5)
        before = self.snapshot(), self.preserved(), len(self.fake.calls)
        with running(self.server()) as port:
            self.screen('history', port, question_id=q['question_id'])
        self.assertEqual((self.snapshot(), self.preserved(), len(self.fake.calls)), before)

    def test_real_accept_dispatch_observe_citations_keep_one_run_and_head(self):
        self.active()
        before = self.preserved(), len(self.fake.posts('/api/agent-runs'))
        with running(self.server()) as port:
            self.assertEqual(self.screen('ask', port)['QA_POST_requests'], 3)
        self.assertEqual(self.preserved(), before[0])
        self.assertEqual(len(self.fake.posts('/api/agent-runs')), before[1] + 1)
        q = self.qa.question_list()['items'][0]
        self.assertEqual((UUID(q['question_id']).version, UUID(q['target_id']).version), (5, 5))
        self.assertEqual((q['state'], q['attempts'], q['head_revision']), ('complete', 1, 1))

    def test_v5_action_lost_reply_reconnect_GET_and_explicit_observe_do_not_redispatch(self):
        self.active()
        before = self.preserved(), len(self.fake.posts('/api/agent-runs'))
        with running(self.server()) as port:
            self.assertEqual(self.screen('lost-dispatch', port)['QA_POST_requests'], 3)
        self.assertEqual(self.preserved(), before[0])
        self.assertEqual(len(self.fake.posts('/api/agent-runs')), before[1] + 1)
        self.assertEqual(len(self.qa.question_list()['items']), 1)
        self.assertEqual(self.qa.question_list()['items'][0]['attempts'], 1)

    def test_real_active_head_paused_policy_is_preparation_state_without_error(self):
        self.active()
        before = self.snapshot(), self.preserved(), len(self.fake.calls)
        with running(self.server(False)) as port:
            self.screen('paused', port)
        self.assertEqual((self.snapshot(), self.preserved(), len(self.fake.calls)), before)


if __name__ == '__main__':
    unittest.main()
