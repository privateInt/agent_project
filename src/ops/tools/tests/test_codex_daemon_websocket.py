"""U01-transport-v1: exactly four offline tests; never open a real socket.

The product is loaded unchanged with an in-memory socket superclass. Installed
websocket-client 1.9.0 performs the real HTTP handshake and frame decoding.
Business-script writes are intercepted BEFORE the compiled fixture executes.
"""
import ast
import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import struct
import sys
import unittest
from unittest import mock

import websocket

from src.ops.tools import codex_daemon_websocket as product


WORK = Path(__file__).resolve().parents[4]
REPO = WORK
SOURCE = WORK / "src/ops/tools/codex_daemon_websocket.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures/codex_daemon_websocket"
OPERATION = FIXTURES / "operation.py.txt"
ORIGINAL_OPERATION = FIXTURES / "original-operation.py.txt"
ENDPOINT = "/fixture/agent-project-control.sock"
THREADS = (
    "01a13f12-328d-7342-b285-a0868ac66447",
    "01a13fb0-1721-7af3-82e6-747eac4de411",
    "01a13fb0-1abe-7623-a15e-cf07071ce54a",
)
FIELDS = (
    "cwd", "approvalPolicy", "approvalsReviewer", "sandbox",
    "activePermissionProfile", "runtimeWorkspaceRoots", "model",
    "modelProvider", "reasoningEffort",
)
METHODS = frozenset({"initialize", "configRequirements/read",
                     "account/rateLimits/read", "thread/resume",
                     "thread/settings/update"})
PRIVATE = "FAKE_PRIVATE_BODY_MUST_NOT_ESCAPE"
OBSERVATIONS = []


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


def frame_header(length, opcode=1, fin=True):
    first = (0x80 if fin else 0) | opcode
    if length < 126:
        return bytes((first, length))
    if length < 65536:
        return bytes((first, 126)) + struct.pack("!H", length)
    return bytes((first, 127)) + struct.pack("!Q", length)


def frame(data, opcode=1, fin=True):
    return frame_header(len(data), opcode, fin) + data


def response(request_id=1, result=None):
    return frame(encoded({"id": request_id, "result": {} if result is None else result}))


def outgoing_frame(data):
    """Decode only fake-captured client sends, not the server decoder under test."""
    opcode, length, pos = data[0] & 15, data[1] & 127, 2
    if length == 126:
        length, pos = struct.unpack("!H", data[2:4])[0], 4
    elif length == 127:
        length, pos = struct.unpack("!Q", data[2:10])[0], 10
    if not data[1] & 128:
        raise AssertionError("Client frames must be masked")
    mask, pos = data[pos:pos + 4], pos + 4
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(data[pos:]))
    if len(payload) != length:
        raise AssertionError("Unexpected partial fake send")
    return opcode, json.loads(payload)


class MemoryStream:
    def __init__(self, data):
        self.data, self.position, self.reads = data, 0, []

    def recv(self, size):
        self.reads.append((self.position, size))
        out = self.data[self.position:self.position + size]
        self.position += len(out)
        if not out:
            raise AssertionError("Decoder read beyond supplied fixture")
        return out


class TransportFixture:
    def __init__(self, wire=b"", *, status=101, delays=None, chunk=None,
                 header_padding=0, failure=None):
        self.clock = 0.0
        self.wire, self.status, self.chunk = wire, status, chunk
        self.delays, self.header_padding = delays or {}, header_padding
        self.failure = failure
        self.sockets, self.connects, self.ws_options = [], [], []
        self.rpc_messages, self.frame_reads, self.http_requests = [], [], []
        self.forbidden, self.close_calls, self.graceful_calls = [], 0, 0
        self.clients, self.timeout_values = [], []
        self.stack = contextlib.ExitStack()

    def forbidden_io(self, *args, **kwargs):
        self.forbidden.append("REAL_IO_ATTEMPT")
        raise AssertionError("Real IO is forbidden by this fixture")

    def delay(self, stage, sock):
        if self.failure == stage:
            raise OSError(PRIVATE)
        amount = self.delays.get(stage, 0)
        if amount:
            if amount >= sock.timeout:
                self.clock += sock.timeout
                raise TimeoutError(PRIVATE)
            self.clock += amount

    def __enter__(self):
        owner = self

        class FakeSocketBase:
            def __init__(sock, family, kind):
                if (family, kind) != (socket.AF_UNIX, socket.SOCK_STREAM):
                    raise AssertionError("Only a fake AF_UNIX stream is allowed")
                sock.timeout, sock.closed = None, False
                sock.http, sock.http_position, sock.frame_position = b"", 0, 0
                owner.sockets.append(sock)

            def settimeout(sock, timeout):
                sock.timeout = timeout
                owner.timeout_values.append(timeout)

            def gettimeout(sock):
                return sock.timeout

            def connect(sock, endpoint):
                owner.connects.append(endpoint)
                if endpoint != ENDPOINT:
                    raise AssertionError("Unapproved fixture endpoint")
                owner.delay("connect", sock)

            def send(sock, data, flags=0):
                data = bytes(data)
                if data.startswith(b"GET "):
                    owner.delay("upgrade_send", sock)
                    owner.http_requests.append(data)
                    key = next(line.split(b": ", 1)[1] for line in data.split(b"\r\n")
                               if line.lower().startswith(b"sec-websocket-key:"))
                    accept = base64.b64encode(hashlib.sha1(
                        key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                    sock.http = (
                        ("HTTP/1.1 %d Fixture\r\n" % owner.status).encode()
                        + b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        + b"Sec-WebSocket-Accept: " + accept + b"\r\n"
                        + b"Location: ws://must-never-resolve.invalid/\r\n"
                        + b"X-Fixture: " + b"x" * owner.header_padding + b"\r\n\r\n")
                else:
                    owner.delay("send", sock)
                    owner.rpc_messages.append(outgoing_frame(data))
                return len(data)

            def recv(sock, size, flags=0):
                stage = "upgrade_recv" if sock.handshaking else "recv"
                owner.delay(stage, sock)
                if sock.handshaking:
                    out = sock.http[sock.http_position:sock.http_position + size]
                    sock.http_position += len(out)
                else:
                    owner.frame_reads.append((sock.frame_position, size))
                    size = min(size, owner.chunk) if owner.chunk else size
                    out = owner.wire[sock.frame_position:sock.frame_position + size]
                    sock.frame_position += len(out)
                return out

            def close(sock):
                owner.close_calls += 1
                sock.closed = True

        # Execute the exact source bytes with a fake superclass: no FD exists.
        spec = importlib.util.spec_from_file_location(
            "src.ops.tools._codex_ws_offline_fixture", SOURCE)
        self.module = importlib.util.module_from_spec(spec)
        with mock.patch.object(socket, "socket", FakeSocketBase):
            spec.loader.exec_module(self.module)
        self.stack.enter_context(mock.patch.object(self.module.time, "monotonic",
                                                  lambda: self.clock))
        for name in ("socket", "create_connection", "getaddrinfo", "socketpair"):
            self.stack.enter_context(mock.patch.object(socket, name, self.forbidden_io))
        self.stack.enter_context(mock.patch("subprocess.Popen", self.forbidden_io))
        real_class, real_connect = websocket.WebSocket, websocket.WebSocket.connect

        def connect(ws, url, **kwargs):
            self.ws_options.append((url, dict(kwargs)))
            return real_connect(ws, url, **kwargs)

        def graceful(*args, **kwargs):
            self.graceful_calls += 1
            raise AssertionError("Graceful close would start a new wait")

        self.stack.enter_context(mock.patch.object(real_class, "connect", connect))
        self.stack.enter_context(mock.patch.object(real_class, "close", graceful))
        return self

    def open(self, **kwargs):
        client = self.module.UnixWebSocketRpc(ENDPOINT, THREADS, **kwargs)
        self.clients.append(client)
        return client

    def __exit__(self, *exc):
        try:
            for client in self.clients:
                client.close()
            if self.forbidden or self.graceful_calls:
                raise AssertionError("Forbidden IO or graceful close attempted")
            if any(not s.closed for s in self.sockets):
                raise AssertionError("Owned fake socket was not closed")
        finally:
            self.stack.close()


def baseline(reviewer="user"):
    return {"cwd": str(REPO), "approvalPolicy": "on-request",
            "approvalsReviewer": reviewer,
            "sandbox": {"type": "workspaceWrite", "networkAccess": False},
            "activePermissionProfile": None, "runtimeWorkspaceRoots": [str(REPO)],
            "model": "fixture-model", "modelProvider": "fixture-provider",
            "reasoningEffort": "fixture-effort"}


def compiled_business_fixture():
    """Compile the pinned raw fixture, adapting only its REPO assignment."""
    raw = OPERATION.read_bytes()
    raw_sha = hashlib.sha256(raw).hexdigest()
    expected_sha = "8a7308e1f3529f118065c7a256be3a3c82b970f9378d9ffdc67d3ef8bd7e647a"
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    if raw_sha != expected_sha or manifest[OPERATION.name]["sha256"] != expected_sha:
        raise AssertionError("Operation fixture raw pin changed")
    text = raw.decode("utf-8")
    tree = ast.parse(text, filename=str(OPERATION))
    assignments = [n for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "REPO" for t in n.targets)]
    if len(assignments) != 1:
        raise AssertionError("Expected exactly one top-level REPO assignment")
    assignment = assignments[0]
    if (len(assignment.targets) != 1 or assignment.col_offset != 0
            or assignment.lineno != assignment.end_lineno
            or not isinstance(assignment.value, ast.Call)
            or ast.unparse(assignment.value.func) != "pathlib.Path"
            or len(assignment.value.args) != 1 or assignment.value.keywords
            or not isinstance(assignment.value.args[0], ast.Constant)
            or not isinstance(assignment.value.args[0].value, str)):
        raise AssertionError("Unexpected REPO assignment shape")
    lines = text.splitlines(keepends=True)
    index = assignment.lineno - 1
    before = lines[index]
    if before != ast.get_source_segment(text, assignment) + "\n":
        raise AssertionError("REPO adapter must replace exactly one complete line")
    after = "REPO=pathlib.Path(" + repr(str(REPO)) + ")\n"
    adapted_lines = list(lines)
    adapted_lines[index] = after
    adapted = "".join(adapted_lines)
    if lines[:index] + lines[index + 1:] != adapted_lines[:index] + adapted_lines[index + 1:]:
        raise AssertionError("Adapter changed bytes outside REPO assignment")
    assignment.value = ast.parse("pathlib.Path(" + repr(str(REPO)) + ")", mode="eval").body
    if ast.dump(tree) != ast.dump(ast.parse(adapted)):
        raise AssertionError("Adapter changed AST outside REPO assignment")
    adapter = {"fixture": str(OPERATION), "raw_sha256": raw_sha,
               "adapted_sha256": hashlib.sha256(adapted.encode("utf-8")).hexdigest(),
               "assignment_line": index + 1, "before": before, "after": after,
               "changed_line_count": int(before != after),
               "only_REPO_assignment_adapted": True,
               "all_other_source_bytes_and_ast_preserved": True}
    return compile(adapted, str(OPERATION), "exec"), adapter


def run_business(*, fail=None, quota=True, reviewers=None, boundary=None,
                 mismatch=None, managed_allowed=True):
    """Execute the actual pinned fixture with fake transport and memory-only IO."""
    fixture_code, fixture_adapter = compiled_business_fixture()
    events, snapshots, writes, chmods, replacements, forbidden = [], [], [], [], [], []
    states = {tid: baseline((reviewers or {}).get(tid, "user")) for tid in THREADS}
    if boundary:
        states[THREADS[2]].update(boundary)
    reads = {tid: 0 for tid in THREADS}
    destination = REPO / "var/unattended-20261015/runtime-policy-websocket-attempt-2.json"
    temporary = destination.with_suffix(".new")

    def no_io(*args, **kwargs):
        forbidden.append("REAL_IO_ATTEMPT")
        raise AssertionError("Business fixture attempted real IO")

    class Client:
        def __init__(self, endpoint, threads, *, total_timeout, request_timeout):
            if (endpoint, tuple(threads), total_timeout, request_timeout) != (
                    "/home/codex_shshin/.codex/app-server-control/app-server-control.sock",
                    THREADS, 60, 5):
                raise AssertionError("Business constructor scope changed")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            events.append(("close", {}))

        def notify_initialized(self):
            events.append(("initialized", {}))

        def call(self, method, params):
            if method not in METHODS:
                raise AssertionError("Business method scope widened")
            events.append((method, copy.deepcopy(params)))
            if method == "initialize":
                if fail == "initialize":
                    raise product.ProbeError("RPC_REQUEST_TIMEOUT")
                return {}
            if method == "configRequirements/read":
                return {"requirements": {"allowedApprovalsReviewers":
                        ["user", "auto_review"] if managed_allowed else ["user"]}}
            if method == "account/rateLimits/read":
                return {"ordinaryUsageAllowed": quota,
                        "rateLimitsByLimitId": {"fixture": {"primary": None}}}
            tid = params.get("threadId")
            if tid not in THREADS:
                raise AssertionError("Business thread scope widened")
            if method == "thread/settings/update":
                if params != {"threadId": tid, "approvalsReviewer": "auto_review"}:
                    raise AssertionError("Business update payload widened")
                states[tid]["approvalsReviewer"] = "auto_review"
                if fail == "update_ack":
                    raise product.ProbeError("RPC_REQUEST_TIMEOUT")
                return {}
            if params != {"threadId": tid, "excludeTurns": True}:
                raise AssertionError("Business resume payload widened")
            reads[tid] += 1
            if reads[tid] > 1 and fail == "readback":
                raise product.ProbeError("RPC_RECEIVE_FAILED")
            value = copy.deepcopy(states[tid])
            if reads[tid] > 1 and mismatch:
                key, changed = mismatch
                value[key] = changed
            return value

    def write_text(path, data, *args, **kwargs):
        if path != temporary:
            no_io()
        writes.append(data)
        snapshots.append(json.loads(data))
        return len(data)

    def chmod(path, mode, *args, **kwargs):
        if (path, mode) != (temporary, 0o600):
            no_io()
        chmods.append(mode)

    def replace(path, target):
        if (path, target) != (temporary, destination):
            no_io()
        replacements.append(str(target))
        return target

    output, previous_path = io.StringIO(), list(sys.path)
    exit_code = 0
    try:
        with contextlib.ExitStack() as stack:
            # Install all guards BEFORE exec and the fixture's top-level write().
            stack.enter_context(mock.patch.object(product, "UnixWebSocketRpc", Client))
            stack.enter_context(mock.patch.object(Path, "write_text", write_text))
            stack.enter_context(mock.patch.object(Path, "chmod", chmod))
            stack.enter_context(mock.patch.object(Path, "replace", replace))
            for name in ("socket", "create_connection", "getaddrinfo", "socketpair"):
                stack.enter_context(mock.patch.object(socket, name, no_io))
            stack.enter_context(mock.patch("subprocess.Popen", no_io))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(output))
            try:
                exec(fixture_code, {"__name__": "__offline_business_fixture__",
                                    "__file__": str(OPERATION), "__package__": None})
            except SystemExit as exc:
                exit_code = exc.code
    finally:
        sys.path[:] = previous_path
    if forbidden:
        raise AssertionError("Real business IO attempted")
    if not snapshots or len(writes) != len(chmods) or len(writes) != len(replacements):
        raise AssertionError("Memory checkpoint write protocol changed")
    return {"record": snapshots[-1], "snapshots": snapshots, "events": events,
            "exit_code": exit_code, "stdout": output.getvalue(), "forbidden": forbidden,
            "fixture_adapter": fixture_adapter}


class CodexDaemonWebSocketTests(unittest.TestCase):
    def assert_code(self, code, operation):
        with self.assertRaises(product.ProbeError) as caught:
            operation()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(code, str(caught.exception))
        self.assertNotIn(PRIVATE, str(caught.exception))
        return caught.exception

    def test_unix_ws_handshake_and_json_messages(self):
        notification = frame(encoded({"method": "fixture/status", "params": {}}))
        data = encoded({"id": 1, "result": {"ok": True}})
        wire = notification + frame(data[:10], fin=False) + frame(data[10:], opcode=0)
        with TransportFixture(wire, chunk=3) as fixture:
            with fixture.open() as client:
                self.assertEqual({"ok": True}, client.call("initialize", {}))
                self.assertEqual(1, client.notifications)
                self.assertEqual([ENDPOINT], fixture.connects)
                url, options = fixture.ws_options[0]
                self.assertEqual("ws://localhost/", url)
                self.assertIs(options["socket"], fixture.sockets[0])
                self.assertEqual(0, options["redirect_limit"])
                self.assertIs(True, options["suppress_origin"])
                self.assertEqual(["*"], options["http_no_proxy"])
                self.assertNotIn(b"Origin:", fixture.http_requests[0])
                self.assertEqual(101, client.ws.getstatus())
                self.assertEqual([(1, {"id": 1, "method": "initialize", "params": {}})],
                                 fixture.rpc_messages)
        for status, code in ((301, "RPC_UPGRADE_REJECTED"),
                             (302, "RPC_UPGRADE_REJECTED"),
                             (200, "RPC_CONNECT_FAILED"),
                             (403, "RPC_CONNECT_FAILED")):
            with self.subTest(status=status), TransportFixture(status=status) as fixture:
                self.assert_code(code, fixture.open)
                self.assertEqual([ENDPOINT], fixture.connects)
                self.assertEqual([], fixture.rpc_messages)
                self.assertEqual(1, len(fixture.ws_options))
        # Also test the explicit status guard independently of library rejection.
        with TransportFixture() as fixture:
            with mock.patch.object(websocket.WebSocket, "getstatus", return_value=200):
                self.assert_code("RPC_UPGRADE_REJECTED", fixture.open)
            self.assertEqual([], fixture.rpc_messages)
        malformed = (
            ("utf8", frame(b"\xff"), "RPC_RECEIVE_FAILED"),
            ("json", frame(b"{"), "RPC_INVALID_JSON"),
            ("duplicate_key", frame(b'{"id":1,"id":1,"result":{}}'), "RPC_DUPLICATE_JSON_KEY"),
            ("nonfinite", frame(b'{"id":1,"result":{"n":NaN}}'), "RPC_NONFINITE_JSON"),
            ("binary", frame(b"{}", opcode=2), "RPC_TEXT_REQUIRED"),
            ("shape", frame(b"[]"), "RPC_RESPONSE_INVALID"),
            ("string_id", frame(b'{"id":"1","result":{}}'), "RPC_RESPONSE_ID_MISMATCH"),
            ("bool_id", frame(b'{"id":true,"result":{}}'), "RPC_RESPONSE_ID_MISMATCH"),
            ("wrong_id", response(2), "RPC_RESPONSE_ID_MISMATCH"),
            ("missing_id", frame(b'{"result":{}}'), "RPC_RESPONSE_ID_MISMATCH"),
            ("result", frame(b'{"id":1,"result":[]}'), "RPC_RESULT_INVALID"),
            ("error", frame(encoded({"id": 1, "error": {"message": PRIVATE}})), "RPC_REMOTE_ERROR"),
            ("server_request", frame(encoded({"method": "approval/request", "id": 7,
                                               "params": {"body": PRIVATE}})),
             "RPC_SERVER_REQUEST_FORBIDDEN"),
        )
        for name, wire, code in malformed:
            with self.subTest(malformed=name), TransportFixture(wire) as fixture:
                client = fixture.open()
                self.assert_code(code, lambda: client.call("initialize", {}))
                self.assertEqual(["initialize"], [p["method"] for _, p in fixture.rpc_messages])
        with TransportFixture(response(1) + response(1)) as fixture:
            client = fixture.open()
            self.assertEqual({}, client.call("initialize", {}))
            self.assert_code("RPC_DUPLICATE_RESPONSE",
                             lambda: client.call("configRequirements/read", {}))
            self.assertFalse(any(p["method"] == "thread/settings/update"
                                 for _, p in fixture.rpc_messages))
        OBSERVATIONS.append({"test": "handshake_json", "malformed_cases": len(malformed),
                             "actual_http_and_fragment_decoder": True})
        self.case_exponent_overflow_rejected()

    def case_exponent_overflow_rejected(self):
        """F1: isolated additional nonfinite subcase of the first method."""
        wire = frame(b'{"id":1,"result":{"n":1e400}}')
        self.finding_observation = {"finding": "F1", "input_number": "1e400",
                                    "expected_error": "RPC_NONFINITE_JSON"}
        with TransportFixture(wire) as fixture:
            client = fixture.open()

            def receive_overflow():
                value = client.call("initialize", {})
                self.finding_observation.update(
                    returned_result=True, returned_n_type=type(value.get("n")).__name__,
                    returned_n_repr=repr(value.get("n")))
                return value

            try:
                self.assert_code("RPC_NONFINITE_JSON", receive_overflow)
            finally:
                self.finding_observation.update(
                    wire_methods=[p["method"] for _, p in fixture.rpc_messages],
                    fake_connect_count=len(fixture.connects),
                    real_io_attempts=len(fixture.forbidden))
            self.assertEqual(["initialize"], self.finding_observation["wire_methods"])
            self.assertEqual(0, self.finding_observation["real_io_attempts"])

    def test_absolute_deadlines_and_bounds(self):
        for stage, code in (("connect", "RPC_CONNECT_FAILED"),
                            ("upgrade_send", "RPC_CONNECT_FAILED"),
                            ("upgrade_recv", "RPC_CONNECT_FAILED")):
            with self.subTest(delay=stage), TransportFixture(delays={stage: 10}) as fixture:
                self.assert_code(code, fixture.open)
                self.assertEqual(5, fixture.clock)
                self.assertEqual([ENDPOINT], fixture.connects)
                self.assertEqual([], fixture.rpc_messages)
        for stage, code in (("send", "RPC_SEND_FAILED"), ("recv", "RPC_RECEIVE_FAILED")):
            with self.subTest(delay=stage), TransportFixture(response(), delays={stage: 10}) as fixture:
                client = fixture.open()
                self.assert_code(code, lambda: client.call("initialize", {}))
                self.assertEqual(5, fixture.clock)
                self.assertTrue(all(0 < x <= 5 for x in fixture.timeout_values))
                before = (fixture.clock, len(fixture.frame_reads), len(fixture.rpc_messages))
                client.close()
                self.assertEqual(before, (fixture.clock, len(fixture.frame_reads),
                                          len(fixture.rpc_messages)))
        with TransportFixture() as fixture:
            client = fixture.open()
            self.assert_code("RPC_RECEIVE_FAILED", lambda: client.call("initialize", {}))
            self.assertEqual(0, fixture.clock)
        with TransportFixture(header_padding=16384) as fixture:
            self.assert_code("RPC_WIRE_LIMIT", fixture.open)
            self.assertEqual(16384, fixture.sockets[0].header_received)
            self.assertEqual(16384, fixture.sockets[0].http_position)
            self.assertEqual([], fixture.rpc_messages)
        # Actual decoder gets only the huge declaration plus an unread sentinel.
        header = frame_header(product.MAX_MESSAGE + 1)
        with TransportFixture(header + b"PAYLOAD_MUST_NOT_BE_READ") as fixture:
            client = fixture.open()
            self.assert_code("RPC_LINE_LIMIT", lambda: client.call("initialize", {}))
            self.assertEqual(len(header), fixture.sockets[0].frame_position)
            self.assertEqual([(0, 2), (2, 8)], fixture.frame_reads)
        first = frame(b"a" * 65536, fin=False)
        second = frame_header(65537, opcode=0)
        with TransportFixture(first + second + b"SECOND_PAYLOAD_MUST_NOT_BE_READ") as fixture:
            client = fixture.open()
            self.assert_code("RPC_LINE_LIMIT", lambda: client.call("initialize", {}))
            self.assertEqual(len(first) + len(second), fixture.sockets[0].frame_position)
            self.assertTrue(all(pos < len(first) + len(second)
                                for pos, _ in fixture.frame_reads))
        with TransportFixture() as fixture:
            client = fixture.open()
            # Isolate actual BoundedFrames' cumulative payload guard from the
            # stricter total-wire guard, which otherwise fires first.
            packet = frame(b"a" * product.MAX_MESSAGE)
            stream = MemoryStream(packet * 4 + frame_header(1) + b"UNREAD")
            decoder = type(client.ws.frame_buffer)(stream.recv, False)
            for _ in range(4):
                self.assertEqual(product.MAX_MESSAGE, len(decoder.recv_frame().data))
            self.assert_code("RPC_STDOUT_LIMIT", decoder.recv_frame)
            self.assertEqual(product.MAX_WIRE, decoder.payload_bytes - 1)
            self.assertEqual(len(packet) * 4 + 2, stream.position)
        # Total HTTP + frame bytes are bounded independently of decoded payload.
        payload = encoded({"method": "fixture/notice", "params": {"pad": "x" * 131000}})
        with TransportFixture(frame(payload) * 5) as fixture:
            client = fixture.open()
            self.assert_code("RPC_WIRE_LIMIT", lambda: client.call("initialize", {}))
            self.assertEqual(product.MAX_WIRE, fixture.sockets[0].received)
            self.assertLessEqual(client.stdout_bytes, product.MAX_WIRE)
        notice = frame(encoded({"method": "fixture/notice", "params": {}}))
        for count, error in ((32, None), (33, "RPC_NOTIFICATION_LIMIT")):
            with self.subTest(notifications=count), TransportFixture(notice * count + response()) as fixture:
                client = fixture.open()
                if error:
                    self.assert_code(error, lambda: client.call("initialize", {}))
                else:
                    self.assertEqual({}, client.call("initialize", {}))
                self.assertEqual(count, client.notifications)
        with TransportFixture(notice * 32 + response(), delays={"recv": 0.5}) as fixture:
            client = fixture.open()
            self.assert_code("RPC_RECEIVE_FAILED", lambda: client.call("initialize", {}))
            self.assertEqual(5, fixture.clock)
            self.assertEqual(5, client.request_deadline)
            self.assertLess(client.notifications, 32)
        with TransportFixture(response(), delays={"recv": 0.5}, chunk=1) as fixture:
            client = fixture.open()
            self.assert_code("RPC_RECEIVE_FAILED", lambda: client.call("initialize", {}))
            self.assertEqual(5, fixture.clock)
            self.assertEqual(5, client.request_deadline)
        with TransportFixture(b"".join(response(i) for i in range(1, 62)),
                              delays={"recv": 0.5}) as fixture:
            client = fixture.open()
            for _ in range(59):
                self.assertEqual({}, client.call("initialize", {}))
            self.assert_code("RPC_RECEIVE_FAILED", lambda: client.call("initialize", {}))
            self.assertEqual(60, fixture.clock)
            self.assertEqual(60, client.deadline)
            client.close()
            self.assertEqual(60, fixture.clock)
        for kwargs in ({"total_timeout": 61}, {"request_timeout": 6},
                       {"total_timeout": 0}, {"request_timeout": 0}):
            with self.subTest(invalid_timeout=kwargs), TransportFixture() as fixture:
                self.assert_code("TIMEOUT_ARGUMENT_INVALID", lambda: fixture.open(**kwargs))
                self.assertEqual([], fixture.connects)
        with TransportFixture() as fixture:
            client = fixture.open()
            self.assert_code("RPC_REQUEST_TOO_LARGE",
                             lambda: client.call("initialize", {"pad": "x" * product.MAX_MESSAGE}))
            self.assertEqual([], fixture.rpc_messages)
        OBSERVATIONS.append({"test": "deadlines_bounds", "oversize_payload_read": 0,
                             "overflow_fragment_payload_read": 0,
                             "real_decoder_payload_limit": product.MAX_WIRE,
                             "absolute_request_seconds": 5, "absolute_total_seconds": 60})

    def case_reviewed_business_diff_guard(self):
        """Audit the original business body plus only the two reviewed F2 lines."""
        old, new = ORIGINAL_OPERATION.read_text(), OPERATION.read_text()
        self.assertEqual("48471f7dc2fc8c2b71ca25684857a9d77a76f87ec3b688dbe2f0e392fb430a98",
                         hashlib.sha256(old.encode()).hexdigest())
        reviewed_replacements = (
            ("    record['settings_changed'].append(tid);record.pop('pending_setting',None);write()\n",
             "    record['settings_changed'].append(tid);write()\n"),
            ("   record['threads'][tid]['after']=after;write()\n",
             "   record['threads'][tid]['after']=after;record.pop('pending_setting',None);write()\n"),
        )
        expected = old
        for before, after in reviewed_replacements:
            self.assertEqual(1, expected.count(before))
            expected = expected.replace(before, after, 1)
            self.assertEqual(1, new.count(after))
        self.assertEqual(expected.split("  client.call('initialize'", 1)[1],
                         new.split("  client.call('initialize'", 1)[1])

        class AllowedTransportChanges(ast.NodeTransformer):
            def visit_Import(self, node):
                return None if [n.name for n in node.names] == ["sys"] else node

            def visit_ImportFrom(self, node):
                return None if node.module == "src.ops.tools.codex_daemon_websocket" else node

            def visit_Expr(self, node):
                if isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == "sys.path.insert":
                    return None
                return self.generic_visit(node)

            def visit_Call(self, node):
                if ast.unparse(node.func) in ("rpc.RpcClient", "UnixWebSocketRpc"):
                    return ast.Call(func=ast.Name(id="APPROVED_TRANSPORT"), args=[], keywords=[])
                return self.generic_visit(node)

            def visit_Constant(self, node):
                if node.value in ("runtime-policy.json", "runtime-policy-websocket-attempt-2.json"):
                    return ast.Constant(value="APPROVED_SEPARATE_OUTPUT")
                return node

        self.assertEqual(ast.dump(AllowedTransportChanges().visit(ast.parse(expected))),
                         ast.dump(AllowedTransportChanges().visit(ast.parse(new))))
        self.reviewed_diff_audit = {
            "original_sha256": hashlib.sha256(old.encode()).hexdigest(),
            "operation_sha256": hashlib.sha256(new.encode()).hexdigest(),
            "exact_reviewed_line_replacements": reviewed_replacements,
            "all_other_business_body_bytes_equal": True,
            "normalized_full_ast_equal": True,
        }

    def test_exact_methods_threads_and_reviewer_only_payload(self):
        self.assertEqual(METHODS, product.METHODS)
        self.case_reviewed_business_diff_guard()
        self.case_portable_business_success()
        for reviewers in ({}, {THREADS[1]: "auto_review"},
                          {tid: "auto_review" for tid in THREADS}):
            with self.subTest(reviewers=reviewers):
                outcome = run_business(reviewers=reviewers)
                record, events = outcome["record"], outcome["events"]
                self.assertEqual(0, outcome["exit_code"])
                self.assertEqual("APPLIED_AND_READ_BACK_FOR_SUBSEQUENT_TURNS", record["status"])
                self.assertEqual("NOT_RETROACTIVELY_CHANGED", record["current_already_running_turn"])
                names = [m for m, _ in events]
                self.assertEqual(["initialize", "initialized", "configRequirements/read",
                                  "account/rateLimits/read", "thread/resume", "thread/resume",
                                  "thread/resume"], names[:7])
                self.assertEqual(1, names.count("initialized"))
                expected = [tid for tid in THREADS if reviewers.get(tid) != "auto_review"]
                updates = [p for m, p in events if m == "thread/settings/update"]
                self.assertEqual([{"threadId": tid, "approvalsReviewer": "auto_review"}
                                  for tid in expected], updates)
                self.assertEqual(expected, record["settings_changed"])
                self.assertEqual(set(THREADS), set(record["threads"]))
                for tid, values in record["threads"].items():
                    self.assertEqual(set(FIELDS), set(values["before"]))
                    self.assertEqual(dict(values["before"], approvalsReviewer="auto_review"),
                                     values["after"])
                self.assertFalse(any(record[k] for k in ("sandbox_policy_changed", "global_config_changed",
                                                         "queue_changed", "services_changed")))
        invalid = (
            ("turn/start", {}, "RPC_METHOD_FORBIDDEN"),
            ("queue/create", {}, "RPC_METHOD_FORBIDDEN"),
            ("initialize", None, "RPC_PARAMS_FORBIDDEN"),
            ("configRequirements/read", {"extra": True}, "RPC_PARAMS_FORBIDDEN"),
            ("account/rateLimits/read", {"extra": True}, "RPC_PARAMS_FORBIDDEN"),
            ("thread/resume", {"threadId": "00000000-0000-0000-0000-000000000000",
                               "excludeTurns": True}, "RPC_THREAD_FORBIDDEN"),
            ("thread/resume", {"threadId": THREADS[0], "excludeTurns": False}, "RPC_PARAMS_FORBIDDEN"),
            ("thread/settings/update", {"threadId": THREADS[0], "approvalsReviewer": "auto_review",
                                        "approvalPolicy": "never"}, "RPC_PARAMS_FORBIDDEN"),
            ("thread/settings/update", {"threadId": THREADS[0], "approvalsReviewer": "user"},
             "RPC_PARAMS_FORBIDDEN"),
            ("thread/settings/update", {"threadId": THREADS[0], "approvalsReviewer": "auto_review",
                                        "sandbox": {"networkAccess": True}}, "RPC_PARAMS_FORBIDDEN"),
        )
        with TransportFixture() as fixture:
            client = fixture.open()
            for method, params, code in invalid:
                with self.subTest(method=method, params=params):
                    self.assert_code(code, lambda: client.call(method, params))
            self.assertEqual([], fixture.rpc_messages)
        # Baseline failure stops before all updates; any of the nine readback
        # fields changing unexpectedly cannot be promoted to success.
        for boundary in ({"approvalPolicy": "never"}, {"runtimeWorkspaceRoots": ["/fixture/other"]}):
            with self.subTest(boundary=boundary):
                outcome = run_business(boundary=boundary)
                self.assertEqual(1, outcome["exit_code"])
                self.assertEqual("BLOCKED_OR_UNCERTAIN", outcome["record"]["status"])
                self.assertFalse(any(m == "thread/settings/update" for m, _ in outcome["events"]))
        for key in FIELDS:
            with self.subTest(readback_field=key):
                outcome = run_business(mismatch=(key, "unexpected-fixture-value"))
                self.assertEqual(1, outcome["exit_code"])
                self.assertEqual("SETTING_READBACK_MISMATCH", outcome["record"]["error"])
                self.assertEqual("BLOCKED_OR_UNCERTAIN", outcome["record"]["status"])
                self.assertEqual([THREADS[0]], outcome["record"]["settings_changed"])
        outcome = run_business(managed_allowed=False)
        self.assertEqual("MANAGED_REVIEWER_DISALLOWED", outcome["record"]["error"])
        self.assertFalse(any(m == "thread/settings/update" for m, _ in outcome["events"]))
        OBSERVATIONS.append({"test": "business_scope", "script_body_byte_preserved": True,
                             "normalized_ast_preserved": True, "memory_only_writes": True,
                             "readback_fields_checked": len(FIELDS), "forbidden_wire_sends": 0})

    def case_portable_business_success(self):
        """The real fixture succeeds using only the current clone and memory IO."""
        outcome = run_business()
        record, events = outcome["record"], outcome["events"]
        self.portability_observation = outcome
        self.assertEqual(WORK, REPO)
        self.assertEqual(0, outcome["exit_code"])
        self.assertEqual("APPLIED_AND_READ_BACK_FOR_SUBSEQUENT_TURNS", record["status"])
        self.assertEqual("NOT_RETROACTIVELY_CHANGED", record["current_already_running_turn"])
        self.assertEqual(list(THREADS), record["settings_changed"])
        self.assertEqual(set(THREADS), set(record["threads"]))
        self.assertIsNone(record.get("pending_setting"))
        expected_methods = ["initialize", "initialized", "configRequirements/read",
                            "account/rateLimits/read"] + ["thread/resume"] * 3
        expected_methods += ["thread/settings/update", "thread/resume"] * 3 + ["close"]
        self.assertEqual(expected_methods, [m for m, _ in events])
        self.assertEqual([{"threadId": tid, "approvalsReviewer": "auto_review"} for tid in THREADS],
                         [p for m, p in events if m == "thread/settings/update"])
        for values in record["threads"].values():
            self.assertEqual(set(FIELDS), set(values["before"]))
            self.assertEqual(baseline(), values["before"])
            self.assertEqual(dict(values["before"], approvalsReviewer="auto_review"), values["after"])
            self.assertEqual(str(WORK), values["after"]["cwd"])
            self.assertEqual([str(WORK)], values["after"]["runtimeWorkspaceRoots"])
        self.assertFalse(any(record[k] for k in ("queue_changed", "services_changed",
                                                 "sandbox_policy_changed", "global_config_changed")))
        self.assertEqual([], outcome["forbidden"])
        self.assertTrue(outcome["fixture_adapter"]["only_REPO_assignment_adapted"])
        self.assertTrue(outcome["fixture_adapter"]["all_other_source_bytes_and_ast_preserved"])

    def test_uncertain_update_never_replayed(self):
        cases = (("initialize", "RPC_REQUEST_TIMEOUT", [], None),
                 ("update_ack", "RPC_REQUEST_TIMEOUT", [], THREADS[0]),
                 ("readback", "RPC_RECEIVE_FAILED", [THREADS[0]], THREADS[0]))
        for failure, code, changed, pending in cases:
            with self.subTest(failure=failure):
                outcome = run_business(fail=failure)
                record, events = outcome["record"], outcome["events"]
                self.assertEqual(1, outcome["exit_code"])
                self.assertEqual("BLOCKED_OR_UNCERTAIN", record["status"])
                self.assertEqual(code, record["error"])
                self.assertEqual(changed, record["settings_changed"])
                self.assertEqual(pending, record.get("pending_setting"))
                updates = [p["threadId"] for m, p in events if m == "thread/settings/update"]
                self.assertEqual([] if failure == "initialize" else [THREADS[0]], updates)
                self.assertEqual(1, sum(m == "initialize" for m, _ in events))
                self.assertEqual(1, sum(m == "close" for m, _ in events))
                self.assertNotIn(PRIVATE, outcome["stdout"])
                if failure != "initialize":
                    pending_snapshots = [x for x in outcome["snapshots"] if x.get("pending_setting")]
                    self.assertTrue(pending_snapshots)
                    self.assertTrue(all(x["pending_setting"] == THREADS[0] for x in pending_snapshots))
                    self.assertNotIn("after", record["threads"][THREADS[0]])
                if failure == "initialize":
                    self.assertEqual(["initialize", "close"], [m for m, _ in events])
                self.assertFalse(any(record[k] for k in ("queue_changed", "services_changed",
                                                         "sandbox_policy_changed", "global_config_changed")))
        for quota in (False, None):
            with self.subTest(quota=quota):
                outcome = run_business(quota=quota)
                self.assertIs(quota, outcome["record"]["usage"]["ordinaryUsageAllowed"])
                self.assertTrue(all(m in METHODS | {"initialized", "close"}
                                    for m, _ in outcome["events"]))
                self.assertFalse(outcome["record"]["queue_changed"])
                self.assertFalse(outcome["record"]["services_changed"])
                self.assertFalse(outcome["record"]["sandbox_policy_changed"])
                self.assertEqual([], outcome["forbidden"])
        OBSERVATIONS.append({"test": "uncertainty", "initialize_failure_updates": 0,
                             "ack_loss_pending_preserved": True,
                             "readback_loss_acknowledged_change_preserved": True,
                             "update_replays": 0, "model_or_queue_starts": 0})
        self.case_ack_then_lost_readback_retains_pending()

    def case_ack_then_lost_readback_retains_pending(self):
        """F2: ACK evidence and the unverified target must survive together."""
        outcome = run_business(fail="readback")
        record, events = outcome["record"], outcome["events"]
        updates = [p["threadId"] for m, p in events if m == "thread/settings/update"]
        self.finding_observation = {
            "finding": "F2", "record": record, "events": events,
            "checkpoints": outcome["snapshots"], "exit_code": outcome["exit_code"],
            "updates": updates, "expected_pending_setting": THREADS[0],
            "actual_pending_setting": record.get("pending_setting"),
            "real_io_attempts": len(outcome["forbidden"]),
        }
        self.assertEqual(1, outcome["exit_code"])
        self.assertEqual("BLOCKED_OR_UNCERTAIN", record["status"])
        self.assertEqual("RPC_RECEIVE_FAILED", record["error"])
        self.assertEqual([THREADS[0]], record["settings_changed"])
        self.assertEqual([THREADS[0]], updates)
        self.assertEqual(1, sum(m == "initialize" for m, _ in events))
        self.assertEqual(1, sum(m == "close" for m, _ in events))
        self.assertEqual(["initialize", "initialized", "configRequirements/read",
                          "account/rateLimits/read", "thread/resume", "thread/resume",
                          "thread/resume", "thread/settings/update", "thread/resume", "close"],
                         [m for m, _ in events])
        self.assertTrue(any(x.get("pending_setting") == THREADS[0]
                            for x in outcome["snapshots"]))
        self.assertTrue(any(x.get("settings_changed") == [THREADS[0]]
                            for x in outcome["snapshots"]))
        self.assertTrue(all("after" not in record["threads"][tid] for tid in THREADS))
        self.assertFalse(any(record[k] for k in ("queue_changed", "services_changed",
                                                 "sandbox_policy_changed", "global_config_changed")))
        self.assertEqual([], outcome["forbidden"])
        self.assertNotIn(PRIVATE, outcome["stdout"])
        self.assertEqual(THREADS[0], record.get("pending_setting"),
                         "ACK list must retain its unverified pending target after readback loss")
