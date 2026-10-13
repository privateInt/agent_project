#!/usr/bin/env python3
"""A03 REST/MCP acceptance using Agent-owned diagnostic Tools only."""
import argparse
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "2025-11-25"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, base, token):
        parsed = urllib.parse.urlparse(base)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid Agent URL")
        self.base, self.token = base.rstrip("/"), token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, body=None, expected=200, key=None, extra=None):
        rid = str(uuid.uuid4())
        headers = {"Authorization": "Bearer " + self.token, "X-Request-ID": rid, "Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        if extra:
            headers.update(extra)
        request = urllib.request.Request(self.base + path, method=method, headers=headers,
                                         data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None)
        try:
            response = self.opener.open(request, timeout=40)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            if response.status != expected:
                # Do not print remote bodies that could contain private input.
                raise ValueError(f"{method} {path}: expected {expected}, got {response.status}")
            if response.headers.get("X-Request-ID") != rid:
                raise ValueError("missing request correlation")
            result = json.load(response)
            return result, response.headers

    def post(self, path, body, expected=201, key=None):
        return self.request("POST", path, body, expected, key or str(uuid.uuid4()))[0]

    def get(self, path):
        return self.request("GET", path)[0]

    def mcp(self, method, params):
        result, _ = self.request("POST", "/mcp", {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params},
                                 extra={"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL})
        if "error" in result:
            raise ValueError("MCP protocol request failed: " + method)
        return result["result"]

    def listed(self, name):
        cursor = None
        for _ in range(20):
            result = self.mcp("tools/list", {"cursor": cursor} if cursor else {})
            if any(item["name"] == name for item in result.get("tools", [])):
                return True
            cursor = result.get("nextCursor")
            if not cursor:
                return False
        raise ValueError("MCP listing page limit exceeded")

    def wait(self, call_id):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            call = self.get("/api/tool-calls/" + call_id)
            if call["state"] not in ("accepted", "running"):
                return call
            time.sleep(0.1)
        raise ValueError("Tool wait expired: " + call_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:16100")
    parser.add_argument("--token-file", type=Path, default=ROOT / ".secrets/api-token")
    parser.add_argument("--call-id", help="read a previously successful call after restart; creates nothing")
    args = parser.parse_args()
    api = API(args.url, args.token_file.read_text().strip())
    status = api.get("/api/status")
    if not status.get("ready") or (status.get("stage"), status.get("schema_version")) not in (("A03", 2), ("A04", 3), ("A05", 3), ("A06", 4), ("A07", 5)):
        raise ValueError("A03 service/schema not ready")
    if args.call_id:
        if str(uuid.UUID(args.call_id)) != args.call_id:
            raise ValueError("invalid call ID")
        call = api.wait(args.call_id)
        if call["state"] != "succeeded":
            raise ValueError("existing Tool call was not successful")
        print(json.dumps({"result": "passed", "stage": status["stage"], "mode": "lookup", "tool_call_id": call["tool_call_id"], "tool_version_id": call["tool_version_id"], "request_id": call["request_id"]}, indent=2))
        return
    hello = api.mcp("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "agent-smoke", "version": "a03.1"}})
    if hello["protocolVersion"] != PROTOCOL:
        raise ValueError("unexpected MCP protocol")
    tool = api.post("/api/tools", {"display_name": "A03 인수 진단 Tool", "description": "외부 API를 호출하지 않는 실행 계약 검사"})
    tid = tool["tool_id"]
    version = api.post("/api/tools/" + tid + "/versions", {"expected_revision": 1, "kind": "diagnostic_echo_v1", "binding": {"prefix": "A03:"}, "timeout_ms": 30000})
    vid = version["tool_version_id"]
    name = "t_" + tid.replace("-", "") + "_v1"
    if api.listed(name):
        raise ValueError("draft exposed over MCP")
    body = {"tool_version_id": vid, "arguments": {"text": "독립성 확인"}, "mode": "run"}
    api.post("/api/tool-calls", body, expected=409)
    test = api.post("/api/tool-calls", dict(body, mode="test"), expected=202)
    if api.wait(test["tool_call_id"])["state"] != "succeeded":
        raise ValueError("draft test failed")
    api.post("/api/tool-versions/" + vid + "/publish", {"expected_revision": 1}, expected=200)
    if not api.listed(name):
        raise ValueError("published Tool missing from MCP")
    key = str(uuid.uuid4())
    accepted = api.post("/api/tool-calls", body, expected=202, key=key)
    replay, headers = api.request("POST", "/api/tool-calls", body, expected=202, key=key)
    if accepted != replay or headers.get("Idempotency-Replayed") != "true":
        raise ValueError("idempotent acceptance mismatch")
    api.post("/api/tool-calls", dict(body, arguments={"text": "changed"}), expected=409, key=key)
    api.post("/api/tool-calls", dict(body, arguments={"unexpected": True}), expected=400)
    mcp = api.mcp("tools/call", {"name": name, "arguments": {"invocation_key": key, "arguments": body["arguments"]}})
    call = mcp.get("structuredContent", {})
    if mcp.get("isError") or call.get("tool_call_id") != accepted["tool_call_id"] or call.get("tool_version_id") != vid or call.get("result", {}).get("data", {}).get("text") != "A03:독립성 확인":
        raise ValueError("REST/MCP result identity mismatch")
    if api.get("/api/tool-calls/" + call["tool_call_id"]) != call:
        raise ValueError("MCP result differs from persisted REST result")
    current = api.get("/api/tools/" + tid)
    api.request("PATCH", "/api/tools/" + tid, {"expected_revision": current["revision"], "enabled": False})
    if api.listed(name):
        raise ValueError("disabled Tool still listed")
    api.post("/api/tool-calls", body, expected=409)
    denied = api.mcp("tools/call", {"name": name, "arguments": {"invocation_key": str(uuid.uuid4()), "arguments": body["arguments"]}})
    if not denied.get("isError"):
        raise ValueError("disabled Tool accepted a new MCP call")
    retired = api.post("/api/tool-versions/" + vid + "/retire", {"expected_revision": 2}, expected=200)
    if retired["state"] != "retired":
        raise ValueError("retirement failed")
    again = api.mcp("tools/call", {"name": name, "arguments": {"invocation_key": key, "arguments": body["arguments"]}})
    if again.get("isError") or again.get("structuredContent", {}).get("tool_call_id") != call["tool_call_id"]:
        raise ValueError("retired Tool lost accepted call replay")
    print(json.dumps({"result": "passed", "stage": status["stage"], "mode": "rest_and_mcp", "tool_id": tid,
                      "tool_version_id": vid, "tool_call_id": call["tool_call_id"], "request_id": call["request_id"],
                      "mcp_protocol": PROTOCOL, "tool_disabled_and_retired": True,
                      "checks": ["draft_test", "publish", "list", "input_validation", "idempotency", "key_conflict", "rest_mcp_same_call", "disable", "retire", "replay_after_retire"]}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, TypeError) as exc:
        raise SystemExit("FAIL: " + str(exc))
