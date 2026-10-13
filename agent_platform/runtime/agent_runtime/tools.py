"""Agent-owned fixed adapters; no arbitrary code execution."""
import json
import os
from pathlib import Path
import re
import time


class ToolError(Exception):
    def __init__(self, code, result=None):
        super().__init__(code)
        self.code, self.result = code, result


def _pairs(items):
    out = {}
    for key, value in items:
        if key in out:
            raise ToolError("TOOL_INPUT_INVALID")
        out[key] = value
    return out


def decode(raw):
    if len(raw) > 256 * 1024:
        raise ToolError("TOOL_INPUT_INVALID")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise ToolError("TOOL_INPUT_INVALID") from None
    if not isinstance(value, dict):
        raise ToolError("TOOL_INPUT_INVALID")
    return value


def credential(connection):
    auth = connection.get("auth_type")
    if auth == "none":
        return "", ""
    if auth not in ("bearer", "api_key"):
        raise ToolError("TOOL_INPUT_INVALID")
    ref = connection.get("credential_ref", "")
    revision = connection.get("credential_revision", "")
    if not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", v) for v in (ref, revision)):
        raise ToolError("TOOL_INPUT_INVALID")
    # A single operator-managed, revisioned file. User input is never a path.
    path = os.environ.get("AGENT_TOOL_CREDENTIALS_FILE", "")
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 65536:
            raise ValueError()
        manifest = decode(raw)
        entry = manifest.get(ref, {})
        if not isinstance(entry, dict) or set(entry) != {"revision", "value"}:
            raise ValueError()
        secret = entry["value"]
        if not isinstance(secret, str) or not 1 <= len(secret) <= 4096 or any(c in secret for c in "\r\n\0"):
            raise ValueError()
    except (OSError, ValueError, TypeError, ToolError):
        raise ToolError("CREDENTIAL_NOT_CONFIGURED") from None
    if entry["revision"] != revision:
        raise ToolError("CREDENTIAL_REVISION_UNAVAILABLE")
    return revision, secret


def credential_revision(connection):
    return credential(connection)[0]


def execute(kind, binding, arguments, connection, context, invocation_key=""):
    if kind == "knowledge_search_v1":
        from . import knowledge_tools
        return knowledge_tools.execute(binding, arguments, connection, context)
    if kind == "openapi_http_v1":
        from . import http_tools
        return http_tools.execute(binding, arguments, connection, context, invocation_key)
    if kind == "diagnostic_echo_v1":
        if set(arguments) != {"text"} or not isinstance(arguments["text"], str) or len(arguments["text"]) > 4096:
            raise ToolError("TOOL_INPUT_INVALID")
        if set(binding) - {"prefix", "delay_ms", "fail"}:
            raise ToolError("TOOL_INPUT_INVALID")
        prefix, delay, fail = binding.get("prefix", ""), binding.get("delay_ms", 0), binding.get("fail", False)
        if not isinstance(prefix, str) or len(prefix) > 256 or type(delay) is not int or not 0 <= delay <= 30000 or type(fail) is not bool:
            raise ToolError("TOOL_INPUT_INVALID")
        end = time.monotonic() + delay / 1000
        while time.monotonic() < end:
            if not context.is_active():
                raise ToolError("TOOL_TIMEOUT")
            time.sleep(min(0.02, max(0, end - time.monotonic())))
        if fail:
            raise ToolError("TOOL_DIAGNOSTIC_FAILURE")
        return {"text": prefix + arguments["text"]}
    if kind == "diagnostic_connection_v1":
        if arguments or set(binding) != {"connection_version_id"} or not connection or connection.get("connection_version_id") != binding["connection_version_id"]:
            raise ToolError("TOOL_INPUT_INVALID")
        revision = credential_revision(connection)
        return {"configured": True, "connection_version_id": binding["connection_version_id"], "credential_revision": revision}
    raise ToolError("TOOL_INPUT_INVALID")
