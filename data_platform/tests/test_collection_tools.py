import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


configure = load("configure", "ops/configure.py")
smoke = load("smoke", "tests/live_collection_smoke.py")


class ConfigureTests(unittest.TestCase):
    def config(self):
        return {"services": {"operation": {"environment": {"POSTGRES_URL": "postgres://test", "KAFKA_BROKERS": "kafka:19092", "KAFKA_COMMAND_TOPIC": "ops.operations.requested"}}, "training": {"environment": {"MINIO_ENDPOINT": "minio:9000", "MINIO_ACCESS_KEY": "key", "MINIO_SECRET_KEY": "secret", "MINIO_USE_SSL": "false", "MINIO_BUCKET": "model-artifacts", "HF_TOKEN": "private-hf"}}}, "networks": {"ops-infra": {"name": "ops-infra"}}}

    def test_only_shared_settings_are_copied(self):
        encoded = configure.dotenv(configure.settings(self.config()))
        for forbidden in ("HF_TOKEN", "private-hf", "model-artifacts", "ops.operations.requested"):
            self.assertNotIn(forbidden, encoded)
        self.assertIn("DATA_MINIO_SECRET_KEY='secret'", encoded)

    def test_missing_and_multiline_settings_are_rejected(self):
        for value in ("", None, "secret\nOTHER=1"):
            config = self.config()
            config["services"]["training"]["environment"]["MINIO_SECRET_KEY"] = value
            with self.assertRaises(ValueError):
                configure.settings(config)

    def test_dollar_and_backslash_remain_literal(self):
        result = configure.dotenv({"VALUE": "${NO_EXPAND}\\t'quoted"})
        self.assertEqual(result.splitlines()[1], "VALUE='${NO_EXPAND}\\t\\'quoted'")

    def test_private_file_cannot_replace_existing_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config"
            configure.create_private(path, "original")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                configure.create_private(path, "replacement")
            self.assertEqual(path.read_text(), "original")


class SmokeTests(unittest.TestCase):
    def document(self):
        return {"state": "ready", "sha256": hashlib.sha256(b"abc").hexdigest(), "size_bytes": 3, "processor_image_id": "sha256:" + "a" * 64}

    def test_same_size_wrong_hash_is_rejected(self):
        with self.assertRaises(ValueError):
            smoke.check_content(self.document(), b"abd")
        smoke.check_content(self.document(), b"abc")

    def test_truncated_or_unready_source_is_rejected(self):
        for document, content in ((self.document(), b"ab"), (dict(self.document(), state="pending"), b"abc")):
            with self.assertRaises(ValueError):
                smoke.check_content(document, content)

    def test_atomic_report_retains_replay_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            smoke.save(path, {"idempotency_key": "same-key", "passed": False})
            smoke.save(path, {"idempotency_key": "same-key", "passed": True})
            self.assertEqual(json.loads(path.read_text())["idempotency_key"], "same-key")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_api_credentials_are_rejected(self):
        with self.assertRaises(ValueError):
            smoke.API("http://user:secret@localhost:14000")


class ScopeManifestTests(unittest.TestCase):
    def fixture(self):
        run = {"url": "https://example.com/", "options": {"follow_links": True,
            "include_attachments": True, "max_depth": 1, "max_documents": 3, "max_total_bytes": 100, "max_bytes": 80}}
        seed = {"source_url": run["url"], "final_url": run["url"], "parent_url": "",
            "crawl_depth": 0, "crawl_role": "seed", "content_type": "text/html", "size_bytes": 10,
            "processor_version": "http-scope-v1", "source_type": "crawl"}
        child = dict(seed, source_url="https://example.com/a.pdf", final_url="https://example.com/a.pdf",
            parent_url=run["url"], crawl_depth=1, crawl_role="attachment", content_type="application/pdf")
        return run, [seed, child]

    def test_every_source_parent_and_scope_checked(self):
        run, docs = self.fixture()
        smoke.validate_manifest(run, docs, 2, 1)
        for changes in ({"parent_url": "https://example.com/missing"}, {"crawl_depth": 3},
                        {"final_url": "https://other.example/a"}, {"crawl_role": "other"},
                        {"processor_version": "http-source-v1"}, {"size_bytes": 200}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                smoke.validate_manifest(run, [docs[0], dict(docs[1], **changes)], 2, 1)

    def test_count_and_attachment_expectations_cannot_silently_pass(self):
        run, docs = self.fixture()
        for values, count, attachments in ((docs[:1], 2, 0), (docs[:1], 1, 1), (docs + docs[1:], 1, 0)):
            with self.assertRaises(ValueError):
                smoke.validate_manifest(run, values, count, attachments)
        smoke.validate_manifest({"options": {}}, docs[:1])


class ScopeRunnerTests(unittest.TestCase):
    def test_lost_reply_resumes_original_request_and_checks_all_documents(self):
        self.exercise(lost_reply=True, corrupt=False)

    def test_corrupt_last_document_cannot_pass(self):
        self.exercise(lost_reply=False, corrupt=True)

    def exercise(self, lost_reply, corrupt):
        run_id = "11111111-1111-4111-8111-111111111111"
        op_id = "22222222-2222-4222-8222-222222222222"
        ids = ["33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444"]
        run, docs = ScopeManifestTests().fixture()
        image = "sha256:" + "a" * 64
        for i, doc in enumerate(docs):
            doc.update(document_id=ids[i], crawl_run_id=run_id, state="ready", size_bytes=3,
                       sha256=hashlib.sha256(b"abc").hexdigest(), processor_image_id=image)
        run.update(crawl_run_id=run_id, operation_id=op_id, output_document_id=ids[0],
                   output_document_ids=ids, status="succeeded", phase="completed", submission_status="delivered")
        report = {"schema_version": 1, "idempotency_key": "retained-key", "passed": False,
                  "request": {"name": "scope-test", "url": run["url"], "options": run["options"]},
                  "expect_min_documents": 2, "expect_attachments": 1}
        state = {"lost": False, "keys": [], "downloads": []}
        class FakeAPI:
            def call(self, path, body=None, key=None, **kwargs):
                if path == "/api/crawl-runs" and body is not None:
                    state["keys"].append(key)
                    if body["name"] != "scope-test":
                        return 409, {"error": "conflict"}
                    if lost_reply and not state["lost"]:
                        state["lost"] = True
                        raise OSError("reply lost after acceptance")
                    return 202, {"crawl_run_id": run_id, "operation_id": op_id, "status": "accepted"}
                if path.startswith("/api/crawl-runs/"):
                    return 200, run
                if path.startswith("/api/source-documents/"):
                    index = ids.index(path.split("/")[3])
                    if path.endswith("/content"):
                        state["downloads"].append(ids[index])
                        return 200, (b"bad" if corrupt and index == 1 else b"abc"), {"Content-Disposition": "attachment; filename=original"}
                    return 200, docs[index]
                if path.startswith("/api/operations?"):
                    return 200, {"items": [{"operation_id": op_id, "status": "succeeded"}]}
                if path.startswith("/api/crawl-runs?"):
                    return 200, {"items": [run], "next_cursor": ""}
                if path.startswith("/api/source-documents?"):
                    return 200, {"items": docs, "next_cursor": ""}
                raise AssertionError(path)
        with tempfile.TemporaryDirectory() as directory, patch.object(smoke, "runtimes", return_value=[{"image_id": image, "state": "exited", "running": False}]):
            path = Path(directory) / "report.json"
            smoke.save(path, report)
            if lost_reply:
                with self.assertRaises(OSError):
                    smoke.execute(FakeAPI(), report, path, 10)
                report = json.loads(path.read_text())
            if corrupt:
                with self.assertRaises(ValueError):
                    smoke.execute(FakeAPI(), report, path, 10)
                self.assertFalse(json.loads(path.read_text())["passed"])
            else:
                smoke.execute(FakeAPI(), report, path, 10)
                self.assertTrue(json.loads(path.read_text())["passed"])
            self.assertEqual(state["downloads"], ids)
            self.assertEqual(set(state["keys"]), {"retained-key"})


if __name__ == "__main__":
    unittest.main()
