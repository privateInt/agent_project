"""Bounded, pinned HTTP transport for compiler-owned OpenAPI operations.

No ambient proxy, redirects, retries, remote references or arbitrary URL input.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import threading
import time
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from .tools import ToolError, _pairs, credential

MAX_BODY = 2 * 1024 * 1024
_resolvers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool-dns")
_dns_slots = threading.BoundedSemaphore(4)


def permitted_ip(value):
    try:
        ip = ipaddress.ip_address(value)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if str(ip) in {"100.100.100.200", "168.63.129.16", "fd00:ec2::254"}:
            return False
        if isinstance(ip, ipaddress.IPv6Address) and (ip.sixtofour or ip.teredo):
            return False
        return not (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved)
    except ValueError:
        return False


def resolve(host, port, timeout):
    if not _dns_slots.acquire(blocking=False):
        raise ToolError("TOOL_UPSTREAM_UNAVAILABLE")
    future = _resolvers.submit(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    future.add_done_callback(lambda _: _dns_slots.release())
    try:
        records = future.result(timeout=timeout)
    except FutureTimeout:
        raise ToolError("TOOL_TIMEOUT") from None
    except OSError:
        raise ToolError("TOOL_UPSTREAM_UNAVAILABLE") from None
    addresses = list(dict.fromkeys(item[4][0] for item in records))
    if not addresses or any(not permitted_ip(ip) for ip in addresses):
        raise ToolError("TOOL_TARGET_BLOCKED")
    return addresses[0]


def scalar(value):
    if isinstance(value, str):
        return value
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in (int, float) and not (isinstance(value, float) and not math.isfinite(value)):
        return json.dumps(value, allow_nan=False)
    raise ToolError("TOOL_INPUT_INVALID")


def prepare(binding, arguments, connection, invocation_key):
    op = binding.get("http")
    if not isinstance(op, dict) or not connection or binding.get("connection_version_id") != connection.get("connection_version_id"):
        raise ToolError("TOOL_INPUT_INVALID")
    base = urlsplit(connection.get("base_url", ""))
    host = (base.hostname or "").rstrip(".").lower()
    if base.scheme not in ("http", "https") or not host or base.username or base.password or base.query or base.fragment or "%" in base.netloc or "\\" in base.geturl() or host == "localhost" or host.endswith(".localhost"):
        raise ToolError("TOOL_TARGET_BLOCKED")
    path = op.get("path", "")
    method = op.get("method")
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE") or not path.startswith("/") or any(c in path for c in "?#%\\\r\n\0"):
        raise ToolError("TOOL_INPUT_INVALID")
    if "%" in base.path or any(p in (".", "..") for p in (base.path + path).split("/")):
        raise ToolError("TOOL_TARGET_BLOCKED")
    headers = {"Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "AgentPlatform/a07.1"}
    query = []
    allowed = {"path": set(), "query": set(), "header": set()}
    for parameter in op["parameters"]:
        name, where = parameter["name"], parameter["in"]
        allowed[where].add(name)
        values = arguments.get(where, {})
        if not isinstance(values, dict):
            raise ToolError("TOOL_INPUT_INVALID")
        if name not in values:
            if parameter["required"]:
                raise ToolError("TOOL_INPUT_INVALID")
            continue
        value = values[name]
        if where == "path":
            value = scalar(value)
            if not value or value in (".", "..") or any(c in value for c in "/\\%\r\n\0"):
                raise ToolError("TOOL_INPUT_INVALID")
            path = path.replace("{" + name + "}", quote(value, safe=""))
        elif where == "query":
            query.extend((name, scalar(v)) for v in (value if isinstance(value, list) else [value]))
        else:
            value = scalar(value)
            if any(c in value for c in "\r\n\0") or len(value) > 4096:
                raise ToolError("TOOL_INPUT_INVALID")
            try:
                value.encode("latin-1")
            except UnicodeError:
                raise ToolError("TOOL_INPUT_INVALID") from None
            headers[name] = value
    for where, names in allowed.items():
        values = arguments.get(where, {})
        if not isinstance(values, dict) or set(values) - names:
            raise ToolError("TOOL_INPUT_INVALID")
    props = op.get("input_schema", {}).get("properties", {})
    if set(arguments) - set(props) or "{" in path or "}" in path:
        raise ToolError("TOOL_INPUT_INVALID")
    _, secret = credential(connection)
    auth = connection["auth_type"]
    if auth == "bearer":
        headers["Authorization"] = "Bearer " + secret
    elif auth == "api_key":
        if connection["api_key_in"] == "header":
            headers[connection["api_key_name"]] = secret
        else:
            query.append((connection["api_key_name"], secret))
    if binding.get("forward_idempotency"):
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", invocation_key):
            raise ToolError("TOOL_INPUT_INVALID")
        headers["Idempotency-Key"] = invocation_key
    body = None
    if "body" in arguments:
        body = json.dumps(arguments["body"], ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    target = quote(base.path.rstrip("/"), safe="/") + quote(path, safe="/%")
    if query:
        target += "?" + urlencode(query)
    if len(target) > 16384 or sum(len(k) + len(v) for k, v in headers.items()) > 16384:
        raise ToolError("TOOL_INPUT_INVALID")
    try:
        target.encode("ascii")
        for value in headers.values():
            value.encode("latin-1")
    except UnicodeError:
        raise ToolError("TOOL_INPUT_INVALID") from None
    return base, method, target, headers, body, secret


def _redact(value, secret, depth=0):
    if depth > 32:
        raise ToolError("TOOL_RESPONSE_INVALID")
    if isinstance(value, dict):
        return {_redact(k, secret, depth + 1): _redact(v, secret, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, secret, depth + 1) for v in value]
    if isinstance(value, str) and secret:
        return value.replace(secret, "[REDACTED]").replace(quote(secret, safe=""), "[REDACTED]")
    return value


class PinnedConnection(http.client.HTTPConnection):
    def connect(self):
        raise OSError("implicit reconnect disabled")


def execute(binding, arguments, connection, context, invocation_key):
    base, method, target, headers, body, secret = prepare(binding, arguments, connection, invocation_key)
    deadline = time.monotonic() + min(120, context.time_remaining() or 0)
    remaining = lambda: max(0.001, deadline - time.monotonic())
    if not context.is_active() or time.monotonic() >= deadline:
        raise ToolError("TOOL_TIMEOUT")
    port = base.port or (443 if base.scheme == "https" else 80)
    ip = resolve(base.hostname, port, min(5, remaining()))
    conn = PinnedConnection(base.hostname, port, timeout=min(5, remaining()))
    dispatched = False
    timer = None
    status = 0
    response = None
    active_socket = [None]
    def abort():
        sock = active_socket[0]
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        conn.close()
    try:
        conn.sock = socket.create_connection((ip, port), min(5, remaining()))
        active_socket[0] = conn.sock
        timer = threading.Timer(remaining(), abort)
        timer.daemon = True
        timer.start()
        if base.scheme == "https":
            conn.sock = ssl.create_default_context().wrap_socket(conn.sock, server_hostname=base.hostname)
            active_socket[0] = conn.sock
        if not context.is_active() or time.monotonic() >= deadline:
            raise ToolError("TOOL_TIMEOUT")
        conn.sock.settimeout(remaining())
        dispatched = True
        conn.request(method, target, body=body, headers=headers)
        response = conn.getresponse()
        status = response.status
        if status == 202:
            reference = response.getheader("Location", "")[:2048]
            u = urlsplit(reference)
            reference = urlunsplit((u.scheme, u.netloc, u.path, "", "")) if not u.username and not u.password and u.scheme in ("", "http", "https") else ""
            result = {"status": 202, "data": None, "completion": "accepted_only", "external_reference": _redact(reference, secret)}
            raise ToolError("TOOL_ASYNC_UNSUPPORTED", result)
        if status < 200 or status >= 300:
            if status in (400, 401, 403, 404, 405, 409, 413, 415, 422, 429):
                dispatched = False  # explicit rejection, not an ambiguous transport failure
            raise ToolError("TOOL_HTTP_ERROR", {"status": status, "data": None, "completion": "unknown"})
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise ToolError("TOOL_RESPONSE_INVALID")
        data = bytearray()
        while not response.isclosed():
            if not context.is_active() or time.monotonic() >= deadline:
                raise ToolError("TOOL_TIMEOUT")
            if active_socket[0]:
                active_socket[0].settimeout(remaining())
            chunk = response.read1(min(65536, MAX_BODY + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_BODY:
                raise ToolError("TOOL_RESPONSE_TOO_LARGE")
        if response.length not in (None, 0):
            raise ToolError("TOOL_RESPONSE_INVALID")
        if status == 204:
            parsed = None
        else:
            if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ToolError("TOOL_RESPONSE_INVALID")
            try:
                parsed = json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_pairs,
                                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError, RecursionError, ToolError):
                raise ToolError("TOOL_RESPONSE_INVALID") from None
        result = {"status": status, "data": _redact(parsed, secret), "completion": "completed"}
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > MAX_BODY + 4096:
            raise ToolError("TOOL_RESPONSE_TOO_LARGE")
        return result
    except ToolError as exc:
        if exc.code == "TOOL_ASYNC_UNSUPPORTED":
            raise
        if dispatched and binding.get("effect", "write") == "write":
            raise ToolError("TOOL_EFFECT_UNKNOWN", {"status": status, "data": None, "completion": "unknown"}) from None
        raise
    except (OSError, http.client.HTTPException, ValueError, TypeError, RecursionError):
        if dispatched and binding.get("effect", "write") == "write":
            raise ToolError("TOOL_EFFECT_UNKNOWN", {"status": status, "data": None, "completion": "unknown"}) from None
        code = "TOOL_TIMEOUT" if time.monotonic() >= deadline or not context.is_active() else "TOOL_UPSTREAM_UNAVAILABLE"
        raise ToolError(code) from None
    finally:
        if timer:
            timer.cancel()
        abort()
        if response is not None:
            response.close()
