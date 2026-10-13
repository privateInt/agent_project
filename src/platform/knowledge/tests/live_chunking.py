#!/usr/bin/env python3
"""Opt-in live check using EXISTING canonical IDs; creates two preview runs only."""
import argparse
import json
import sys
import time
import urllib.request
import urllib.error
import uuid


def request(base, path, body=None, key=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Idempotency-Key"] = key
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=105) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:15100")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--canonical-id", action="append")
    source.add_argument("--baseline-run-id", help="Reuse a completed run's exact canonical IDs and size options")
    parser.add_argument("--max-characters", type=int)
    parser.add_argument("--overlap-characters", type=int)
    parser.add_argument("--expected-structure-version", help="Reject an old service before creating runs")
    parser.add_argument("--timeout", type=int, default=660)
    args = parser.parse_args()
    baseline = None
    if args.baseline_run_id:
        baseline = request(args.base_url, "/api/chunking-runs/" + str(uuid.UUID(args.baseline_run_id)))
        if not baseline.get("result_registered"):
            raise ValueError("Baseline must be a completed stored chunk run")
        canonical_ids = [d["canonical_id"] for d in baseline["spec"]["documents"]]
        defaults = baseline["spec"]["chunker"]["options"]
    else:
        canonical_ids = args.canonical_id
        defaults = {"max_characters": 4000, "overlap_characters": 200}
    canonical_ids = [str(uuid.UUID(value)) for value in canonical_ids]
    options = {"max_characters": args.max_characters if args.max_characters is not None else defaults["max_characters"],
               "overlap_characters": args.overlap_characters if args.overlap_characters is not None else defaults["overlap_characters"]}
    maximum, overlap = options["max_characters"], options["overlap_characters"]
    if not 100 <= maximum <= 8000 or not 0 <= overlap < maximum / 2:
        raise ValueError("Maximum must be 100..8000; overlap must be nonnegative and less than half")
    if not 1 <= len(canonical_ids) <= 20 or len(set(canonical_ids)) != len(canonical_ids):
        raise ValueError("Choose 1..20 unique canonical IDs")
    if baseline and options != defaults:
        raise ValueError("Baseline comparison requires identical size options")
    supported = request(args.base_url, "/api/chunkers")
    descriptors = {item["id"]: item for item in supported["items"]}
    expected = {"recursive_character_v1", "structure_v1"}
    if not expected.issubset(descriptors):
        raise RuntimeError("Required chunkers are missing")
    if args.expected_structure_version and descriptors["structure_v1"]["version"] != args.expected_structure_version:
        raise RuntimeError("Structure chunker version differs; update the service before creating runs")
    receipts = []
    for kind in sorted(expected):
        key = "chunk-smoke-" + str(uuid.uuid4())
        body = {"name": "청킹 검증 " + kind, "canonical_ids": canonical_ids,
                "chunker": {"id": kind, "options": options}}
        accepted = request(args.base_url, "/api/chunking-runs", body, key)
        print(json.dumps({"accepted_run_id": accepted["chunking_run_id"], "chunker": kind}), file=sys.stderr, flush=True)
        replay = request(args.base_url, "/api/chunking-runs", body, key)
        if accepted != replay:
            raise RuntimeError("Same-key replay changed the accepted resource")
        run_id = accepted["chunking_run_id"]
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            run = request(args.base_url, "/api/chunking-runs/" + run_id)
            selection = run["spec"]["chunker"]
            if selection["id"] != kind or selection["version"] != descriptors[kind]["version"] or selection["options"] != options:
                raise RuntimeError("Accepted run differs from the selected chunker policy")
            if baseline and (run["spec"]["snapshot_id"] != baseline["spec"]["snapshot_id"] or
                             run["spec"]["documents"] != baseline["spec"]["documents"]):
                raise RuntimeError("Canonical snapshot differs from baseline")
            if run.get("submission_status") == "blocked":
                raise RuntimeError("Operation submission blocked: " + run.get("submission_error_code", ""))
            try:
                operation = request(args.base_url, "/api/operations/" + accepted["operation_id"])
            except urllib.error.HTTPError as exc:
                if exc.code == 404 and run.get("submission_status") == "pending":
                    time.sleep(2)
                    continue
                raise
            if operation["status"] in {"failed", "cancelled"}:
                raise RuntimeError(json.dumps(operation, ensure_ascii=False))
            if run["result_registered"] and operation["status"] == "succeeded":
                break
            time.sleep(2)
        else:
            raise TimeoutError("Chunk result not registered within timeout: " + run_id)
        page = request(args.base_url, "/api/chunking-runs/" + run_id + "/chunks?limit=2")
        if not page["items"] or any(not c["source_spans"] for c in page["items"]):
            raise RuntimeError("Missing chunks or source spans")
        receipts.append({"run_id": run_id, "snapshot_id": run["spec"]["snapshot_id"],
                         "chunker": kind, "chunker_version": selection["version"], "options": options,
                         "policy_hash": selection["policy_hash"],
                         "chunk_count": run["chunk_count"], "sha256": run["result_sha256"]})
    if len({r["snapshot_id"] for r in receipts}) != 1:
        raise RuntimeError("Strategies did not use the same snapshot")
    print(json.dumps({"status": "passed", "scope": "live chunking and replay; no embedding/index/search",
                      "baseline_run_id": args.baseline_run_id, "runs": receipts}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
