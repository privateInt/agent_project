#!/usr/bin/env python3
"""Check the deployed foundation contract; creates only one small Agent diagnostic."""
import argparse
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:16100")
    parser.add_argument("--token-file", type=Path, default=ROOT / ".secrets/api-token")
    parser.add_argument("--check-id", help="read an existing check after restart; creates nothing")
    args = parser.parse_args()
    parsed = urllib.parse.urlparse(args.url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid Agent gateway URL")
    token = args.token_file.read_text().strip()
    if args.check_id and str(uuid.UUID(args.check_id)) != args.check_id:
        raise ValueError("invalid check ID")
    # Do not forward the bearer token to a redirect destination.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(NoRedirect())

    def request(method, path, expected, auth=True):
        rid = str(uuid.uuid4())
        headers = {"X-Request-ID": rid}
        if auth:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(args.url.rstrip("/") + path, method=method,
                                     data=b"{}" if method == "POST" else None, headers=headers)
        try:
            response = opener.open(req, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            if response.status != expected:
                raise ValueError(f"{method} {path}: expected {expected}, got {response.status}")
            if response.headers.get("X-Request-ID") != rid:
                raise ValueError("request correlation header missing")
            return json.load(response)

    request("GET", "/healthz", 200, False)
    request("GET", "/readyz", 200, False)
    request("GET", "/api/status", 401, False)
    status = request("GET", "/api/status", 200)
    if status.get("ready") is not True or status.get("schema_version") not in (1, 2, 3, 4, 5):
        raise ValueError("Agent status/schema not ready")
    if args.check_id:
        check = request("GET", "/api/system-checks/" + args.check_id, 200)
    else:
        check = request("POST", "/api/system-checks", 201)
        saved = request("GET", "/api/system-checks/" + check["check_id"], 200)
        if saved != check:
            changed = sorted(key for key in set(check) | set(saved)
                             if key not in check or key not in saved or check[key] != saved[key])
            raise ValueError("persisted diagnostic changed: check_id=" + check["check_id"]
                             + " fields=" + ",".join(changed))
    if check.get("state") != "succeeded" or not check.get("runtime_instance_id"):
        raise ValueError("runtime diagnostic failed")
    print(json.dumps({"result": "passed", "stage": status.get("stage"), "check_id": check["check_id"],
                      "request_id": check["request_id"], "mode": "lookup" if args.check_id else "create_and_lookup"}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError) as exc:
        raise SystemExit("FAIL: " + str(exc))
