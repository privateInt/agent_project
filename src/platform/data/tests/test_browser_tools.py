"""Browser runner integrity and Linux network filter; no browser dependency."""
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("collection_smoke", ROOT / "tests/live_collection_smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class BrowserToolsTests(unittest.TestCase):
    def test_fixture_requires_real_dom_change(self):
        url, fixture = smoke.browser_fixture()
        raw = base64.b64decode(url.split("/base64/", 1)[1])
        self.assertEqual(raw, fixture["html"].encode())
        self.assertNotIn(fixture["marker"].encode(), raw)
        with self.assertRaises(ValueError):
            smoke.verify_browser_fixture({"browser_fixture": fixture}, raw, raw)
        smoke.verify_browser_fixture({"browser_fixture": fixture}, b"<p>" + fixture["marker"].encode() + b"</p>", raw)

    def test_response_hash_and_metrics_cannot_silently_pass(self):
        capture = {"representation": "rendered_dom", "response_sha256": hashlib.sha256(b"raw").hexdigest(),
                   "response_size_bytes": 3, "network_requests": 2, "network_bytes": 5}
        headers = {"Content-Disposition": "attachment; filename=source"}
        smoke.check_browser_capture({"browser_capture": capture}, b"raw", headers)
        for changes in ({"response_sha256": "bad"}, {"response_size_bytes": 4}, {"representation": "http"}, {"network_requests": 129}, {"network_bytes": 2}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                smoke.check_browser_capture({"browser_capture": dict(capture, **changes)}, b"raw", headers)

    def test_changed_resume_request_stops_before_api(self):
        class NoAPI:
            def call(self, *args, **kwargs):
                raise AssertionError("changed request reached API")
        report = {"request": {"url": "https://example.com/", "options": {"render_javascript": True}}, "idempotency_key": "same"}
        report["browser_request_sha256"] = smoke.browser_request_hash(report)
        for field in ("request", "idempotency_key", "expect_min_documents"):
            changed = copy.deepcopy(report)
            changed[field] = {"options": {"render_javascript": False}} if field == "request" else "changed"
            with self.assertRaises(ValueError):
                smoke.execute(NoAPI(), changed, Path("unused"), 10)

    def test_lost_acceptance_reply_keeps_original_browser_request(self):
        calls = []
        class LostAPI:
            def call(self, path, body=None, key=None, **kwargs):
                if body is None:
                    return 200, {"items": []}
                calls.append((copy.deepcopy(body), key))
                raise OSError("reply lost")
        report = {"request": {"name": "browser", "url": "https://example.com/", "options": {"render_javascript": True}}, "idempotency_key": "retained", "api": "http://127.0.0.1:14000"}
        report["browser_request_sha256"] = smoke.browser_request_hash(report)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            smoke.save(path, report)
            for _ in range(2):
                loaded = json.loads(path.read_text())
                with self.assertRaises(OSError):
                    smoke.execute(LostAPI(), loaded, path, 10)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])

    def test_network_filter_is_inherited_by_exec_child(self):
        # Linux-only job contract; fail rather than silently skip isolation.
        module = str(ROOT / "jobs/collection/browser/browser.py")
        code = "import importlib.util,subprocess,sys; s=importlib.util.spec_from_file_location('browser',sys.argv[1]); m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.isolate_network(); child=subprocess.run([sys.executable,'-c',\"import socket; socket.socket(socket.AF_INET,socket.SOCK_STREAM)\"],capture_output=True); assert child.returncode != 0 and b'Operation not permitted' in child.stderr"
        result = subprocess.run([sys.executable, "-c", code, module], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
