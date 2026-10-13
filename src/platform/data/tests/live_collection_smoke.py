#!/usr/bin/env python3
"""Bounded public URL collection -> CPU Job -> verified originals; standard library."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class API:
    def __init__(self, base):
        parts = urllib.parse.urlsplit(base)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("--api must be an HTTP(S) origin without credentials")
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, path, body=None, key=None, timeout=20, binary=False):
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if key:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            response = self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            data = response.read((32 << 20) + 1)
            if len(data) > 32 << 20:
                raise ValueError("API response exceeded the source size limit")
            if binary:
                return response.status, data, dict(response.headers)
            return response.status, json.loads(data)


def save(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + str(uuid.uuid4()))
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def expect(code, expected, result):
    if code != expected:
        raise ValueError("Unexpected API status %s (expected %s): %s" % (code, expected, result))


def check_content(document, content):
    if document.get("state") != "ready" or document.get("size_bytes") != len(content):
        raise ValueError("Original is not ready or its size differs")
    if document.get("sha256") != hashlib.sha256(content).hexdigest():
        raise ValueError("Downloaded original hash differs")
    if not document.get("processor_image_id", "").startswith("sha256:"):
        raise ValueError("Processor image identity is missing")


def runtimes(operation_id, run_id, execution_type="collection.crawl"):
    listed = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=data-platform.owner=dispatcher", "--filter", "label=data-platform.operation_id=" + operation_id],
        check=True, capture_output=True, text=True, timeout=15,
    ).stdout.split()
    if not listed:
        return []
    inspected = json.loads(subprocess.run(["docker", "inspect", *listed], check=True, capture_output=True, text=True, timeout=15).stdout)
    out = []
    for runtime in inspected:
        labels = runtime.get("Config", {}).get("Labels", {})
        host = runtime.get("HostConfig", {})
        if labels.get("data-platform.operation_id") != operation_id or labels.get("data-platform.resource_id") != run_id or labels.get("data-platform.execution_type") != execution_type:
            raise ValueError("Runtime ownership mismatch")
        if host.get("DeviceRequests") or host.get("Privileged") or host.get("RestartPolicy", {}).get("Name") != "no":
            raise ValueError("Crawl runtime is not a CPU one-shot Job")
        out.append({"id": runtime["Id"], "image_id": runtime["Image"], "state": runtime["State"]["Status"], "running": runtime["State"]["Running"], "exit_code": runtime["State"]["ExitCode"], "oom_killed": runtime["State"]["OOMKilled"]})
    return out


def execute(api, report, path, timeout):
    validate_browser_report(report)
    report["passed"] = False
    deadline = time.monotonic() + timeout
    readiness_deadline = min(deadline, time.monotonic() + 120)
    announced = False
    while time.monotonic() < readiness_deadline:
        ready = True
        for collection in ("crawl-runs", "operations"):
            try:
                code, _ = api.call("/api/" + collection + "?limit=1", timeout=10)
                ready = ready and code == 200
            except (OSError, ValueError):
                ready = False
        if ready:
            break
        if not announced:
            print("[waiting] Collection and Operation APIs are starting; no Job submitted yet.", flush=True)
            announced = True
        time.sleep(2)
    else:
        raise ValueError("Collection and Operation APIs did not become ready; no Job submitted")
    code, accepted = api.call("/api/crawl-runs", report["request"], report["idempotency_key"])
    expect(code, 202, accepted)
    for name in ("crawl_run_id", "operation_id"):
        if report.get(name) and report[name] != accepted.get(name):
            raise ValueError("Resumed request changed its identity")
        report[name] = str(uuid.UUID(accepted[name]))
    save(path, report)
    print("[request]", json.dumps(accepted), flush=True)
    code, replay = api.call("/api/crawl-runs", report["request"], report["idempotency_key"])
    expect(code, 202, replay)
    if replay != accepted:
        raise ValueError("Idempotent replay changed response")
    code, conflict = api.call("/api/crawl-runs", dict(report["request"], name="changed-" + report["request"]["name"]), report["idempotency_key"])
    expect(code, 409, conflict)
    report["idempotent_replay"] = True
    previous = None
    run = None
    while time.monotonic() < deadline:
        code, run = api.call("/api/crawl-runs/" + report["crawl_run_id"])
        expect(code, 200, run)
        state = {k: run.get(k) for k in ("status", "phase", "submission_status", "error_code")}
        if state != previous:
            print("[collection]", json.dumps(state), flush=True)
            previous = state
        report["last_run"] = run
        save(path, report)
        if run.get("status") == "failed" or run.get("submission_status") == "blocked":
            raise ValueError("Crawl failed or submission blocked: " + json.dumps(run))
        if run.get("status") == "succeeded":
            break
        time.sleep(2)
    else:
        raise ValueError("Crawl did not complete within %ss; repeat using --resume with this record" % timeout)
    if report["request"].get("options", {}).get("render_javascript") and (not run.get("options", {}).get("render_javascript") or run.get("url") != report["request"]["url"]):
        raise ValueError("Saved browser request was not executed as browser capture")
    document_ids = run.get("output_document_ids") or [run["output_document_id"]]
    if not isinstance(document_ids, list) or not document_ids or len(document_ids) != len(set(document_ids)):
        raise ValueError("Missing or duplicate source manifest")
    if document_ids[0] != run["output_document_id"]:
        raise ValueError("Seed document differs from manifest")
    documents = []
    for value in document_ids:
        document_id = str(uuid.UUID(value))
        code, document = api.call("/api/source-documents/" + document_id)
        expect(code, 200, document)
        code, body, headers = api.call("/api/source-documents/" + document_id + "/content", binary=True, timeout=90)
        expect(code, 200, "original download")
        check_content(document, body)
        if document.get("document_id") != document_id or document["crawl_run_id"] != report["crawl_run_id"] or not headers.get("Content-Disposition", "").startswith("attachment;"):
            raise ValueError("Original identity or download disposition differs")
        if report.get("browser_fixture") and document.get("crawl_role") == "attachment" and body != b"browser attachment bytes":
            raise ValueError("Dynamic fixture attachment bytes differ")
        capture = document.get("browser_capture")
        if capture:
            code, raw, raw_headers = api.call("/api/source-documents/" + document_id + "/http-response", binary=True, timeout=90)
            expect(code, 200, "initial HTTP response download")
            check_browser_capture(document, raw, raw_headers)
            if document_id == document_ids[0] and report.get("browser_fixture"):
                verify_browser_fixture(report, body, raw)
        documents.append(document)
        print("[document]", json.dumps(document), flush=True)
    validate_manifest(run, documents, report.get("expect_min_documents", 1), report.get("expect_attachments", 0))
    document = documents[0]
    report["document"] = document
    report["documents"] = documents
    # Scope rows share their request name; walk pages instead of assuming the
    # seed sorts into the first two results of a multi-document batch.
    for collection, field, wanted in (("crawl-runs", "crawl_run_id", {report["crawl_run_id"]}), ("source-documents", "document_id", set(document_ids))):
        cursor = ""
        found = set()
        for _ in range(30):
            code, listed = api.call("/api/" + collection + "?" + urllib.parse.urlencode({"q": report["request"]["name"], "limit": 20, "cursor": cursor}))
            expect(code, 200, listed)
            found.update(item.get(field) for item in listed["items"])
            if wanted <= found:
                break
            cursor = listed.get("next_cursor", "")
            if not cursor:
                raise ValueError("Result missing from collection list: " + collection)
        else:
            raise ValueError("Result listing exceeded smoke pagination limit")
    code, operations = api.call("/api/operations?" + urllib.parse.urlencode({"resource_type": "crawl_run", "resource_id": report["crawl_run_id"]}))
    expect(code, 200, operations)
    if len(operations["items"]) != 1 or operations["items"][0].get("status") != "succeeded":
        raise ValueError("Crawl has duplicate or unfinished operations")
    if any(k in operations["items"][0] for k in ("dispatch_token", "execution_instance_id", "dispatch_owner")):
        raise ValueError("Execution grant exposed in public response")
    stop_deadline = time.monotonic() + 30
    while time.monotonic() < stop_deadline:
        observed = runtimes(report["operation_id"], report["crawl_run_id"], "collection.browser") if run.get("options", {}).get("render_javascript") else runtimes(report["operation_id"], report["crawl_run_id"])
        if observed and all(r["state"] == "exited" and not r["running"] for r in observed):
            break
        time.sleep(1)
    else:
        raise ValueError("One-shot crawl runtime has not exited")
    if any(r.get("exit_code", 0) != 0 or r.get("oom_killed", False) for r in observed):
        raise ValueError("Crawl Job exited unsuccessfully")
    if len(observed) != 1 or any(d["processor_image_id"] != observed[0]["image_id"] for d in documents):
        raise ValueError("Source processor image differs from observed runtime")
    report["runtimes"] = observed
    report["passed"] = True
    save(path, report)
    if report.get("browser_fixture"):
        print("PASS: JavaScript changed the DOM, created an attachment link, and both DOM/HTTP response hashes verified.")
    print("PASS: URL collection, immutable request replay, all %d original hashes, CPU Job exit and public result lookup." % len(documents))


def validate_manifest(run, documents, minimum=1, attachments=0):
    options = run.get("options", {})
    expanded = options.get("follow_links") or options.get("include_attachments") or options.get("render_javascript")
    if not expanded:
        if len(documents) != 1 or minimum > 1 or attachments:
            raise ValueError("Single URL result did not meet expectations")
        return
    maximum = options.get("max_documents", 0)
    if not minimum <= len(documents) <= maximum:
        raise ValueError("Collected document count did not meet scope/expectation")
    origin = urllib.parse.urlsplit(run["url"])
    seen = set()
    parents = {}
    total = 0
    attachment_count = 0
    for index, doc in enumerate(documents):
        source = doc.get("source_url", "")
        final = doc.get("final_url", "")
        if source in seen:
            raise ValueError("Duplicate requested URL in manifest")
        seen.add(source)
        for url in (source, final):
            target = urllib.parse.urlsplit(url)
            if target.scheme not in ("http", "https") or target.hostname != origin.hostname or (origin.scheme == "https" and target.scheme != "https"):
                raise ValueError("Result escaped requested hostname")
        role = doc.get("crawl_role")
        depth = doc.get("crawl_depth")
        if not isinstance(depth, int) or isinstance(depth, bool):
            raise ValueError("Invalid source depth")
        if doc.get("processor_version") != ("chromium-dom-v1" if options.get("render_javascript") else "http-scope-v1") or doc.get("source_type") != "crawl":
            raise ValueError("Unexpected source processor/origin")
        if index == 0:
            if source != run["url"] or role != "seed" or depth != 0 or doc.get("parent_url") != "":
                raise ValueError("Invalid seed provenance")
        else:
            parent = doc.get("parent_url")
            if parent not in parents or depth - 1 not in parents[parent]:
                raise ValueError("Invalid source parent/depth")
            if role == "attachment":
                if not options.get("include_attachments") or depth > options.get("max_depth", 0) + 1:
                    raise ValueError("Unrequested attachment")
                attachment_count += 1
            elif role == "page":
                if not options.get("follow_links") or depth > options.get("max_depth", 0):
                    raise ValueError("Unrequested page depth")
            else:
                raise ValueError("Invalid source role")
        if role != "attachment" and doc.get("content_type", "").split(";", 1)[0] == "text/html":
            parents.setdefault(final, set()).add(depth)
        if not 0 <= doc["size_bytes"] <= options.get("max_bytes", 0):
            raise ValueError("Source exceeds requested per-document limit")
        if role == "page" and doc.get("content_type", "").split(";", 1)[0] != "text/html":
            raise ValueError("Page result is not HTML")
        capture = doc.get("browser_capture")
        needs_capture = options.get("render_javascript") and role != "attachment"
        if bool(capture) != bool(needs_capture):
            raise ValueError("DOM/response provenance missing or assigned to an attachment")
        if capture:
            if capture.get("representation") != "rendered_dom" or doc["content_type"] != "text/html; charset=utf-8":
                raise ValueError("Browser capture representation differs")
            total += capture["response_size_bytes"]
        total += doc["size_bytes"]
    if total > options.get("max_total_bytes", 0) or attachment_count < attachments:
        raise ValueError("Total bytes or attachment expectation failed")
    print("[scope]", json.dumps({"documents": len(documents), "attachments": attachment_count,
        "total_bytes": total, **{k: run.get(k, False) for k in ("document_limit_reached", "depth_limit_reached", "link_limit_reached")}}), flush=True)


def browser_request_hash(report):
    value = {key: report.get(key) for key in ("api", "idempotency_key", "request", "browser_fixture", "expect_min_documents", "expect_attachments")}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def validate_browser_report(report):
    if not report.get("request", {}).get("options", {}).get("render_javascript") and not report.get("browser_request_sha256"):
        return
    if report.get("browser_request_sha256") != browser_request_hash(report):
        raise ValueError("Saved browser request or expectations changed; no API request made")
    if report.get("browser_fixture"):
        url, fixture = browser_fixture()
        if report["request"]["url"] != url or report["browser_fixture"] != fixture:
            raise ValueError("Authored browser fixture differs; no API request made")


def check_browser_capture(document, raw, headers):
    capture = document["browser_capture"]
    if capture.get("representation") != "rendered_dom" or capture.get("response_size_bytes") != len(raw) or capture.get("response_sha256") != hashlib.sha256(raw).hexdigest():
        raise ValueError("Initial HTTP response size/hash/provenance differs")
    if not headers.get("Content-Disposition", "").startswith("attachment;"):
        raise ValueError("HTTP response must download as an attachment")
    if not 1 <= capture.get("network_requests", 0) <= 128 or not len(raw) <= capture.get("network_bytes", -1) <= 32 << 20:
        raise ValueError("Browser network metrics exceed bounds")


def browser_fixture():
    import base64
    def encoded(text):
        return base64.b64encode(text.encode()).decode()
    marker = "BROWSER-DYNAMIC-CONTENT-20260915"
    # Split the marker so its complete text cannot occur in initial HTML.
    body = "<!doctype html><html><head><meta charset=utf-8><link rel=icon href=data:,></head><body><p id=output>initial</p><script>setTimeout(()=>{document.getElementById('output').textContent=['BROWSER','DYNAMIC','CONTENT','20260915'].join('-');const a=document.createElement('a');a.href='/base64/" + encoded("browser attachment bytes") + "';a.download='evidence.txt';a.textContent='dynamic attachment';document.body.append(a)},100)</script></body></html>"
    return "https://httpbin.org/base64/" + encoded(body), {"html": body, "marker": marker}


def verify_browser_fixture(report, dom, raw):
    fixture = report["browser_fixture"]
    if raw != fixture["html"].encode() or fixture["marker"].encode() in raw or fixture["marker"].encode() not in dom or dom == raw:
        raise ValueError("JavaScript fixture did not produce the expected DOM from preserved HTTP bytes")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:14000")
    parser.add_argument("--url", default="https://example.com/")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--render-javascript", action="store_true")
    parser.add_argument("--render-wait-ms", type=int, default=0)
    parser.add_argument("--browser-fixture", action="store_true", help="Authored JavaScript fixture through public httpbin; verifies changed DOM and dynamic attachment")
    parser.add_argument("--follow-links", action="store_true")
    parser.add_argument("--include-attachments", action="store_true")
    parser.add_argument("--max-depth", type=int, default=0)
    parser.add_argument("--max-documents", type=int, default=0)
    parser.add_argument("--max-total-bytes", type=int, default=0)
    parser.add_argument("--expect-min-documents", type=int, default=1)
    parser.add_argument("--expect-attachments", type=int, default=0)
    args = parser.parse_args()
    if args.timeout < 1 or args.timeout > 3600:
        parser.error("timeout must be 1..3600 seconds")
    if not 1 <= args.expect_min_documents <= 20 or not 0 <= args.expect_attachments <= 19:
        parser.error("expected document counts are outside the collection limits")
    if args.resume and (args.render_javascript or args.render_wait_ms or args.browser_fixture or args.follow_links or args.include_attachments or args.max_depth or args.max_documents or args.max_total_bytes or args.expect_min_documents != 1 or args.expect_attachments):
        parser.error("--resume uses saved scope and expectations; do not supply new scope options")
    if args.resume:
        path = args.resume.resolve()
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("schema_version") != 1:
            raise ValueError("Unsupported smoke record")
        api = API(report["api"])
    else:
        key = str(uuid.uuid4())
        path = Path(__file__).resolve().parent / "results" / "collection" / (key + ".json")
        api = API(args.api)
        report = {"schema_version": 1, "api": api.base, "idempotency_key": key, "passed": False,
                  "request": {"name": "collection-smoke-" + key, "url": args.url, "options": {"max_bytes": 8388608, "timeout_seconds": 120}}}
        options = report["request"]["options"]
        if args.render_javascript or args.follow_links or args.include_attachments:
            if not 0 <= args.max_depth <= 3 or (not args.follow_links and args.max_depth):
                parser.error("max-depth must be 0..3 and requires --follow-links")
            if not 0 <= args.max_documents <= 20 or args.max_total_bytes and not 8388608 <= args.max_total_bytes <= 67108864:
                parser.error("scope count/byte limits invalid")
            options.update(follow_links=args.follow_links, include_attachments=args.include_attachments,
                           max_depth=args.max_depth, max_documents=args.max_documents, max_total_bytes=args.max_total_bytes)
        elif args.max_depth or args.max_documents or args.max_total_bytes:
            parser.error("scope limits require --follow-links or --include-attachments")
        if args.render_javascript:
            if not 0 <= args.render_wait_ms <= 10000:
                parser.error("render-wait-ms must be 0..10000")
            options.update(render_javascript=True, render_wait_ms=args.render_wait_ms)
        elif args.render_wait_ms:
            parser.error("render-wait-ms requires --render-javascript")
        if args.browser_fixture:
            if args.render_javascript or args.follow_links or args.include_attachments or args.max_depth or args.max_documents or args.max_total_bytes or args.render_wait_ms or args.url != "https://example.com/":
                parser.error("--browser-fixture supplies its own URL, scope and wait settings")
            report["request"]["url"], report["browser_fixture"] = browser_fixture()
            options.update(render_javascript=True, render_wait_ms=1500, include_attachments=True, max_documents=2)
            args.expect_min_documents, args.expect_attachments = 2, 1
        report["expect_min_documents"] = args.expect_min_documents
        report["expect_attachments"] = args.expect_attachments
        if options.get("render_javascript"):
            report["browser_request_sha256"] = browser_request_hash(report)
        save(path, report)  # Save key BEFORE the first POST, including lost replies.
    print("[record]", path, flush=True)
    try:
        execute(api, report, path, args.timeout)
    except Exception:
        report["passed"] = False
        save(path, report)
        raise


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit("FAIL: " + str(exc))
