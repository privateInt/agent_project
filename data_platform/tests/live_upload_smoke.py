#!/usr/bin/env python3
"""Upload originals, verify immutable replay and downloads; no crawling or GPU Job."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from live_collection_smoke import API, expect, save

MAX_BYTES = 32 << 20
FORMATS = {".txt": "text", ".md": "markdown", ".markdown": "markdown", ".html": "html", ".htm": "html",
           ".pdf": "pdf", ".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".docx": "docx", ".xlsx": "xlsx"}


def office_fixture(kind):
    if kind == "docx":
        main = "word/document.xml"
        main_type = "wordprocessingml.document"
        xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Upload original</w:t></w:r></w:p></w:body></w:document>'
        extra = {}
    elif kind == "xlsx":
        main = "xl/workbook.xml"
        main_type = "spreadsheetml.sheet"
        xml = '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
        extra = {
            "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/worksheets/sheet1.xml": '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Upload original</t></is></c></row></sheetData></worksheet>',
        }
    else:
        raise ValueError("Unsupported fixture")
    override = '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' if kind == "xlsx" else ""
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/' + main + '" ContentType="application/vnd.openxmlformats-officedocument.' + main_type + '.main+xml"/>' + override + '</Types>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="' + main + '"/></Relationships>',
        main: xml,
    }
    parts.update(extra)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for filename, content in sorted(parts.items()):
            info = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content.encode("utf-8"))
    return buf.getvalue()


def source_bytes(item):
    if item.get("path"):
        with Path(item["path"]).open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
    elif item["fixture"] == "text":
        data = "원본 문서\r\n  공백과 줄바꿈을 그대로 보존합니다.\t123\n".encode("utf-8")
    else:
        data = office_fixture(item["fixture"])
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Upload source must contain 1..33554432 bytes")
    return data


def checked_source(item):
    data = source_bytes(item)
    if item["sha256"] != hashlib.sha256(data).hexdigest() or item["size_bytes"] != len(data):
        raise ValueError("Source changed since the report was saved; original replay was not sent")
    return data


def multipart(name, filename, data):
    boundary = "data-upload-" + uuid.uuid4().hex
    if "\r" in filename or "\n" in filename:
        raise ValueError("Invalid filename")
    disposition = "Content-Disposition: form-data; name=\"file\"; filename*=UTF-8''" + urllib.parse.quote(filename, safe="")
    body = ("--" + boundary + '\r\nContent-Disposition: form-data; name="name"\r\n\r\n' + name + "\r\n--" + boundary + "\r\n" + disposition + "\r\nContent-Type: application/octet-stream\r\n\r\n").encode("utf-8")
    body += data + ("\r\n--" + boundary + "--\r\n").encode("ascii")
    return body, "multipart/form-data; boundary=" + boundary


def upload(api, item, data, name=None):
    body, content_type = multipart(item["name"] if name is None else name, item["filename"], data)
    request = urllib.request.Request(api.base + "/api/source-documents", data=body, headers={
        "Content-Type": content_type, "Accept": "application/json", "Idempotency-Key": item["key"]})
    try:
        response = api.opener.open(request, timeout=120)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read((1 << 20) + 1)
        if len(raw) > 1 << 20:
            raise ValueError("Oversized upload response")
        return response.status, json.loads(raw)


def check_document(item, document, data):
    if document.get("document_id") != str(uuid.UUID(document["document_id"])):
        raise ValueError("Invalid document identity")
    for field, expected in {"source_type": "upload", "original_filename": item["filename"], "name": item["name"],
                            "format": item["format"], "state": "ready",
                            "sha256": item["sha256"], "size_bytes": len(data), "crawl_run_id": "", "source_url": "", "final_url": ""}.items():
        if document.get(field) != expected:
            raise ValueError("Upload document field mismatch: " + field)
    if document.get("format_version") not in ("source-format-v1", "source-format-v2"):
        raise ValueError("Unsupported upload format version")
    if not document.get("artifact_uri", "").startswith("s3://data-sources/uploads/"):
        raise ValueError("Upload object namespace differs")
    if item["sha256"] != hashlib.sha256(data).hexdigest():
        raise ValueError("Original download differs")


def execute(api, report, path, existing_document_id=None):
    # Check ALL inputs before submitting any request during resume.
    sources = [checked_source(item) for item in report["uploads"]]
    for item, original in zip(report["uploads"], sources):
        code, document = upload(api, item, original)
        expect(code, 200, document)
        check_document(item, document, original)
        if item.get("document") and item["document"] != document:
            raise ValueError("Resumed upload changed its registered result")
        item["document"] = document
        save(path, report)
        code, replay = upload(api, item, original)
        expect(code, 200, replay)
        if replay != document:
            raise ValueError("Replay created a different document")
        code, conflict = upload(api, item, original, name="changed-" + item["name"])
        expect(code, 409, conflict)
        document_id = document["document_id"]
        code, fetched = api.call("/api/source-documents/" + document_id)
        expect(code, 200, fetched)
        if fetched != document:
            raise ValueError("Document detail differs from upload response")
        code, content, headers = api.call("/api/source-documents/" + document_id + "/content", binary=True, timeout=90)
        expect(code, 200, "original download")
        check_document(item, document, content)
        if content != original or not headers.get("Content-Disposition", "").startswith("attachment;") or headers.get("X-Content-Type-Options") != "nosniff":
            raise ValueError("Original bytes or download headers differ")
        code, listing = api.call("/api/source-documents?" + urllib.parse.urlencode({"q": item["name"], "limit": 2}))
        expect(code, 200, listing)
        if len(listing["items"]) != 1 or listing["items"][0]["document_id"] != document_id:
            raise ValueError("Upload replay duplicated a document or listing is missing it")
        code, operations = api.call("/api/operations?" + urllib.parse.urlencode({"resource_id": document_id}))
        expect(code, 200, operations)
        if operations["items"]:
            raise ValueError("Synchronous upload unexpectedly created an Operation")
        item["passed"] = True
        save(path, report)
        print("[upload]", json.dumps({k: document[k] for k in ("document_id", "source_type", "original_filename", "format", "size_bytes", "sha256")}, ensure_ascii=False), flush=True)
    if existing_document_id:
        document_id = str(uuid.UUID(existing_document_id))
        code, document = api.call("/api/source-documents/" + document_id)
        expect(code, 200, document)
        if document.get("source_type") != "crawl" or not document.get("crawl_run_id"):
            raise ValueError("Existing document is not a crawl original")
        code, content, _ = api.call("/api/source-documents/" + document_id + "/content", binary=True, timeout=90)
        expect(code, 200, "existing crawl original")
        if len(content) != document["size_bytes"] or hashlib.sha256(content).hexdigest() != document["sha256"]:
            raise ValueError("Existing crawl document integrity differs")
        report["existing_crawl_document"] = document
        print("[existing-crawl]", document_id, "PASS", flush=True)
    report["passed"] = True
    save(path, report)
    print("PASS: file upload, format recognition, immutable replay, original download and no document Operation.")


def make_report(api, files):
    key = str(uuid.uuid4())
    report = {"schema_version": 1, "kind": "upload-smoke", "api": api.base, "idempotency_key": key, "passed": False, "uploads": []}
    inputs = [{"path": str(p.resolve()), "filename": p.name} for p in files] if files else [
        {"fixture": "text", "filename": "original.txt"}, {"fixture": "docx", "filename": "original.docx"}, {"fixture": "xlsx", "filename": "original.xlsx"}]
    for index, item in enumerate(inputs):
        suffix = Path(item["filename"]).suffix.lower()
        if suffix not in FORMATS:
            raise ValueError("Unsupported source extension")
        data = source_bytes(item)
        item.update({"key": key + "-" + str(index), "name": "upload-smoke-" + key + "-" + str(index),
                     "format": FORMATS[suffix], "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)})
        report["uploads"].append(item)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:14000")
    parser.add_argument("--file", type=Path, action="append", default=[])
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--existing-document-id")
    args = parser.parse_args()
    if args.resume and args.file:
        parser.error("--resume uses only the files recorded in the report")
    if args.resume:
        path = args.resume.resolve()
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("schema_version") != 1 or report.get("kind") != "upload-smoke":
            raise ValueError("Unsupported upload smoke report")
        api = API(report["api"])
    else:
        api = API(args.api)
        report = make_report(api, args.file)
        path = Path(__file__).resolve().parent / "results" / "uploads" / (report["idempotency_key"] + ".json")
    report["passed"] = False
    if args.existing_document_id:
        report["existing_document_id"] = str(uuid.UUID(args.existing_document_id))
    save(path, report)  # Before the first POST, including lost replies.
    print("[record]", path, flush=True)
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                code, _ = api.call("/api/source-documents?limit=1", timeout=5)
                if code == 200:
                    break
            except (OSError, ValueError):
                pass
            if time.monotonic() >= deadline:
                raise ValueError("Collection API not ready; no upload submitted")
            time.sleep(2)
        execute(api, report, path, report.get("existing_document_id"))
    except Exception:
        report["passed"] = False
        save(path, report)
        raise


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as exc:
        raise SystemExit("FAIL: " + str(exc))
