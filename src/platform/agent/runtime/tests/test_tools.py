import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent_runtime.tools import execute, ToolError


class Context:
    def is_active(self):
        return True


class DiagnosticToolTest(unittest.TestCase):
    def test_echo_and_failure_are_fixed_adapters(self):
        self.assertEqual(execute("diagnostic_echo_v1", {"prefix": "확인:"}, {"text": "한글"}, None, Context()), {"text": "확인:한글"})
        for kind, binding, arguments, error in [
            ("python", {}, {}, "TOOL_INPUT_INVALID"),
            ("diagnostic_echo_v1", {"fail": True}, {"text": "x"}, "TOOL_DIAGNOSTIC_FAILURE"),
            ("diagnostic_echo_v1", {}, {"text": "x", "url": "http://localhost"}, "TOOL_INPUT_INVALID")]:
            with self.assertRaisesRegex(ToolError, error):
                execute(kind, binding, arguments, None, Context())

    def test_credential_revision_is_checked_and_never_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.json"
            secret = "private-api-value"
            path.write_text(json.dumps({"test_api": {"revision": "v1", "value": secret}}))
            connection = {"connection_version_id": "fixed", "auth_type": "bearer", "credential_ref": "test_api", "credential_revision": "v1"}
            with patch.dict(os.environ, {"AGENT_TOOL_CREDENTIALS_FILE": str(path)}):
                result = execute("diagnostic_connection_v1", {"connection_version_id": "fixed"}, {}, connection, Context())
                self.assertNotIn(secret, json.dumps(result))
                self.assertEqual(result["credential_revision"], "v1")
                connection["credential_revision"] = "v2"
                with self.assertRaisesRegex(ToolError, "CREDENTIAL_REVISION_UNAVAILABLE"):
                    execute("diagnostic_connection_v1", {"connection_version_id": "fixed"}, {}, connection, Context())
                connection["credential_ref"] = "missing"
                with self.assertRaisesRegex(ToolError, "CREDENTIAL_NOT_CONFIGURED"):
                    execute("diagnostic_connection_v1", {"connection_version_id": "fixed"}, {}, connection, Context())
