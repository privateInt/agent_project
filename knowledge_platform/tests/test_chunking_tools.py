"""Contract checks for baseline-preserving live preview commands; no live calls."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("live_chunking", Path(__file__).with_name("live_chunking.py"))
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)
CANONICAL = "157c335f-5436-4338-8078-edb040ebe1ca"
BASELINE = "6e3eaf24-c2dc-4756-87bb-83468a96c827"


class ChunkingToolsTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.accepted, self.runs = [], {}, {}
        self.versions = {"structure_v1": "2026-09-21.1", "recursive_character_v1": "2026-09-18.2"}
        self.baseline = {"result_registered": True, "spec": {"snapshot_id": "a"*64,
                         "documents": [{"canonical_id": CANONICAL, "canonical_sha256": "b"*64}],
                         "chunker": {"options": {"max_characters": 1000, "overlap_characters": 100}}}}

    def request(self, base, path, body=None, key=None):
        self.calls.append((path, copy.deepcopy(body)))
        if path == "/api/chunkers":
            return {"items": [{"id": k, "version": v} for k,v in self.versions.items()]}
        if path == "/api/chunking-runs/" + BASELINE:
            return copy.deepcopy(self.baseline)
        if path == "/api/chunking-runs" and body is not None:
            if key not in self.accepted:
                kind = body["chunker"]["id"]
                run_id = str(len(self.accepted)+1)
                self.accepted[key] = {"chunking_run_id": run_id, "operation_id": run_id}
                stored = copy.deepcopy(self.baseline)
                stored["spec"]["chunker"] = {"id": kind, "version": self.versions[kind],
                                             "options": body["chunker"]["options"], "policy_hash": "c"*64}
                stored.update(submission_status="delivered", chunk_count=2, result_sha256="d"*64)
                self.runs[run_id] = stored
            return self.accepted[key]
        if path.startswith("/api/operations/"):
            return {"status": "succeeded"}
        if "/chunks?" in path:
            return {"items": [{"source_spans": [{"block_id": "b1"}]}]}
        if path.startswith("/api/chunking-runs/"):
            return copy.deepcopy(self.runs[path.rsplit("/",1)[-1]])
        raise AssertionError("unexpected endpoint: " + path)

    def run_cli(self, *args):
        stdout = io.StringIO()
        with patch.object(live, "request", side_effect=self.request), \
             patch("sys.argv", ["live_chunking.py", *args]), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            live.main()
        return json.loads(stdout.getvalue())

    def test_baseline_pins_options_snapshot_and_two_preview_runs(self):
        original = copy.deepcopy(self.baseline)
        result = self.run_cli("--baseline-run-id", BASELINE, "--expected-structure-version", "2026-09-21.1")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(self.accepted), 2)
        self.assertEqual(self.baseline, original)
        bodies = [b for _,b in self.calls if b is not None]
        self.assertEqual(len(bodies), 4)  # first create and same-key replay for each
        for b in bodies:
            self.assertEqual(b["canonical_ids"], [CANONICAL])
            self.assertEqual(b["chunker"]["options"], {"max_characters":1000,"overlap_characters":100})
        self.assertEqual({r["chunker_version"] for r in result["runs"]}, set(self.versions.values()))

    def test_old_service_rejected_before_creating_any_run(self):
        self.versions["structure_v1"] = "2026-09-18.2"
        with self.assertRaisesRegex(RuntimeError, "version differs"):
            self.run_cli("--baseline-run-id", BASELINE, "--expected-structure-version", "2026-09-21.1")
        self.assertFalse(self.accepted)

    def test_baseline_option_change_and_invalid_limit_rejected_before_mutation(self):
        for args in [("--baseline-run-id",BASELINE,"--max-characters","2000"),
                     ("--canonical-id",CANONICAL,"--max-characters","100","--overlap-characters","50")]:
            with self.assertRaises(ValueError): self.run_cli(*args)
            self.assertFalse(self.accepted)

    def test_changed_source_snapshot_stops_before_second_run(self):
        request = self.request
        def drift(base,path,body=None,key=None):
            result = request(base,path,body,key)
            if path == "/api/chunking-runs/1": result["spec"]["snapshot_id"] = "f"*64
            return result
        self.request = drift
        with self.assertRaisesRegex(RuntimeError,"snapshot differs"):
            self.run_cli("--baseline-run-id",BASELINE)
        self.assertEqual(len(self.accepted),1)


if __name__ == "__main__":
    unittest.main()
