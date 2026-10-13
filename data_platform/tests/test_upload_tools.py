#!/usr/bin/env python3
"""Smoke evidence checks; these do not replace Go/MinIO integration tests."""
import copy
import email.policy
from email.parser import BytesParser
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid
import zipfile

import live_upload_smoke as smoke


class FakeAPI:
    base = "http://127.0.0.1:14000"

    def __init__(self):
        self.saved = {}
        self.corrupt = False
        self.operation = False
        self.replay_wrong = False

    def upload(self, api, item, data, name=None):
        if name is not None and name != item["name"]:
            return 409, {"error_code": "AlreadyExists"}
        if item["key"] in self.saved:
            result = copy.deepcopy(self.saved[item["key"]][0])
            if self.replay_wrong:
                result["document_id"] = str(uuid.uuid4())
            return 200, result
        doc = {"document_id": str(uuid.uuid4()), "source_type": "upload", "name": item["name"],
               "original_filename": item["filename"], "format": item["format"], "format_version": "source-format-v1",
               "state": "ready", "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
               "crawl_run_id": "", "source_url": "", "final_url": "", "artifact_uri": "s3://data-sources/uploads/fixture"}
        self.saved[item["key"]] = (doc, data)
        return 200, copy.deepcopy(doc)

    def call(self, path, **kwargs):
        if path.startswith("/api/operations?"):
            return 200, {"items": [{"operation_id": "unexpected"}] if self.operation else []}
        if "?" in path:
            query = smoke.urllib.parse.parse_qs(smoke.urllib.parse.urlsplit(path).query)["q"][0]
            return 200, {"items": [doc for doc, _ in self.saved.values() if doc["name"] == query]}
        for doc, data in self.saved.values():
            if doc["document_id"] in path:
                if path.endswith("/content"):
                    return 200, b"x" * len(data) if self.corrupt else data, {"Content-Disposition": "attachment; filename=original", "X-Content-Type-Options": "nosniff"}
                return 200, copy.deepcopy(doc)
        raise AssertionError("Unexpected lookup " + path)


class UploadToolsTests(unittest.TestCase):
    def test_office_fixtures_are_deterministic_and_have_real_document_parts(self):
        for kind, main in (("docx", "word/document.xml"), ("xlsx", "xl/workbook.xml")):
            data = smoke.office_fixture(kind)
            self.assertEqual(data, smoke.office_fixture(kind))
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                self.assertIsNone(archive.testzip())
                for part in (main, "[Content_Types].xml", "_rels/.rels"):
                    self.assertTrue(archive.read(part))

    def test_multipart_preserves_unicode_filename_and_original_bytes(self):
        original = b"original\r\n  spaces\t\x00\xff"
        body, kind = smoke.multipart("원본 등록", "문서.txt", original)
        message = BytesParser(policy=email.policy.default).parsebytes(("Content-Type: " + kind + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
        parts = list(message.iter_parts())
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0].get_payload(decode=True).decode(), "원본 등록")
        self.assertEqual(parts[1].get_filename(), "문서.txt")
        self.assertEqual(parts[1].get_payload(decode=True), original)

    def test_smoke_success_and_resume_keep_document_identity(self):
        api = FakeAPI()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            report = smoke.make_report(api, [])
            with mock.patch.object(smoke, "upload", side_effect=api.upload):
                smoke.execute(api, report, path)
                before = copy.deepcopy(report)
                smoke.execute(api, report, path)
            self.assertTrue(report["passed"])
            self.assertEqual(before, report)
            self.assertEqual(len(api.saved), 3)

    def test_corrupt_content_or_extra_operation_cannot_pass(self):
        for field in ("corrupt", "operation", "replay_wrong"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                api = FakeAPI()
                setattr(api, field, True)
                report = smoke.make_report(api, [])
                with mock.patch.object(smoke, "upload", side_effect=api.upload), self.assertRaises(ValueError):
                    smoke.execute(api, report, Path(tmp) / "report.json")
                self.assertFalse(report["passed"])

    def test_changed_source_blocks_all_resume_uploads(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "a.txt", Path(tmp) / "b.txt"
            first.write_bytes(b"a")
            second.write_bytes(b"b")
            api = FakeAPI()
            report = smoke.make_report(api, [first, second])
            second.write_bytes(b"c")
            with mock.patch.object(smoke, "upload") as post, self.assertRaises(ValueError):
                smoke.execute(api, report, Path(tmp) / "report.json")
            post.assert_not_called()

    def test_lost_response_keeps_original_replay_key(self):
        api = FakeAPI()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            report = smoke.make_report(api, [])
            smoke.save(path, report)
            def lost(api_arg, item, data, name=None):
                api.upload(api_arg, item, data, name)
                raise OSError("reply lost")
            with mock.patch.object(smoke, "upload", side_effect=lost), self.assertRaises(OSError):
                smoke.execute(api, report, path)
            retry = json.loads(path.read_text())
            self.assertEqual(retry["idempotency_key"], report["idempotency_key"])
            with mock.patch.object(smoke, "upload", side_effect=api.upload):
                smoke.execute(api, retry, path)
            self.assertTrue(retry["passed"])
            self.assertEqual(len(api.saved), 3)

    def test_ready_flag_alone_does_not_validate_provenance(self):
        api = FakeAPI()
        item = smoke.make_report(api, [])["uploads"][0]
        data = smoke.checked_source(item)
        _, doc = api.upload(api, item, data)
        for field, value in (("source_type", "crawl"), ("format", "pdf"), ("original_filename", "other.txt"), ("sha256", "0" * 64), ("size_bytes", len(data) + 1)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                smoke.check_document(item, dict(doc, **{field: value}), data)

    def test_known_format_versions_allow_immutable_v1_replay_and_v2_upload(self):
        api = FakeAPI()
        item = smoke.make_report(api, [])["uploads"][0]
        data = smoke.checked_source(item)
        _, doc = api.upload(api, item, data)
        for version in ('source-format-v1', 'source-format-v2'):
            smoke.check_document(item, dict(doc, format_version=version), data)
        for version in ('', 'source-format-v99', None):
            with self.assertRaises(ValueError):
                smoke.check_document(item, dict(doc, format_version=version), data)


if __name__ == "__main__":
    unittest.main()
