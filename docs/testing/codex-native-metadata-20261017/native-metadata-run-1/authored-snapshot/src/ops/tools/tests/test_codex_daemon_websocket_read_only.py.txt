"""Native metadata frozen plan+addendum: exactly four new fake-only methods."""
import base64
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import threading
import time
from types import ModuleType
import unittest
from unittest import mock

WORK = Path(__file__).resolve().parents[4]
SOURCE = WORK / "src/ops/tools/codex_daemon_websocket.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures/codex_daemon_websocket_read_only"
LEGACY_METHODS = frozenset({"initialize", "configRequirements/read", "account/rateLimits/read",
                            "thread/resume", "thread/settings/update"})
OBSERVATIONS = []
TARGET_WITNESSES = []
CLEANUPS = []
PRIMITIVES = None
DATA = None


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class ForbiddenIO(BaseException):
    pass


def fixture_context(wire=b"", **options):
    """Keep exact pinned fixture implementation; strengthen its IO exception type."""
    @contextlib.contextmanager
    def run():
        class StrictFixture(PRIMITIVES.TransportFixture):
            def forbidden_io(self, *args, **kwargs):
                self.forbidden.append("REAL_IO_ATTEMPT")
                raise ForbiddenIO("REAL_IO_FORBIDDEN")

            def __enter__(self):
                result = super().__enter__()
                self.sent_frames = []
                actual = PRIMITIVES.outgoing_frame

                def captured(data):
                    decoded = actual(data)
                    self.sent_frames.append(bytes(data))
                    return decoded

                self.stack.enter_context(mock.patch.object(PRIMITIVES, "outgoing_frame", captured))
                self.stack.enter_context(mock.patch.object(threading.Thread, "start", self.forbidden_io))
                self.stack.enter_context(mock.patch.object(time, "sleep", self.forbidden_io))
                return result

            def __exit__(self, *error):
                try:
                    return super().__exit__(*error)
                finally:
                    CLEANUPS.append({"sockets": len(self.sockets), "all_closed": all(sock.closed for sock in self.sockets),
                                     "forbidden": len(self.forbidden), "graceful": self.graceful_calls,
                                     "connects": len(self.connects), "native_calls": 0})

        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with StrictFixture(wire, **options) as fixture:
                yield fixture
        if output.getvalue():
            raise AssertionError("RAW_OUTPUT_FORBIDDEN")
    return run()


def decoded_messages(fixture):
    return [message for opcode, message in fixture.rpc_messages if opcode == 1]


def summary(fixture, client, reason):
    messages = decoded_messages(fixture)
    value = {"reason": reason, "frames": len(messages), "ids": [m.get("id") for m in messages],
             "methods": [m.get("method") for m in messages], "next_id": client.next_id,
             "connects": len(fixture.connects), "close_calls": fixture.close_calls,
             "all_fake_sockets_closed": all(sock.closed for sock in fixture.sockets),
             "real_io_attempts": len(fixture.forbidden), "graceful_calls": fixture.graceful_calls,
             "wire_hashes": [sha(raw) for raw in fixture.sent_frames], "summary_phase": "BEFORE_FIXTURE_EXIT", "native_calls": 0}
    OBSERVATIONS.append(value)
    return value


class ReadOnlyMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global PRIMITIVES, DATA
        manifest = json.loads((FIXTURES / "manifest.json").read_bytes())
        if not manifest["fixture_only"] or manifest["source_test_sha256"] != "5085ce3d866a81afe7614cb6e4aeea785cf757da148395591dbc865ed0f3a9bf":
            raise AssertionError("PINNED_SYNTHETIC_FIXTURE_REQUIRED")
        for name, expected in manifest["files"].items():
            if sha((FIXTURES / name).read_bytes()) != expected:
                raise AssertionError("FIXTURE_PIN_MISMATCH")
        DATA = json.loads((FIXTURES / "synthetic.json").read_bytes())
        if DATA["provenance"] != "SYNTHETIC_ONLY_NATIVE_METADATA_NO_REAL_SOCKET":
            raise AssertionError("SYNTHETIC_ONLY_REQUIRED")
        PRIMITIVES = ModuleType("native_metadata_pinned_primitives")
        raw = (FIXTURES / "transport_primitives.py.txt").read_bytes()
        exec(compile(raw, str(FIXTURES / "transport_primitives.py.txt"), "exec"), PRIMITIVES.__dict__)
        PRIMITIVES.SOURCE = SOURCE
        PRIMITIVES.ENDPOINT = DATA["endpoint"]
        PRIMITIVES.THREADS = (DATA["thread_id"],)
        if PRIMITIVES.websocket.__version__ != "1.9.0":
            raise AssertionError("PINNED_WEBSOCKET_190_REQUIRED")

    def setUp(self):
        self.helper_markers, self.helper_invocations = {}, {}
        self.last_wrong_wire = None

    def error(self, fixture, operation, expected):
        caught = None
        try:
            operation()
        except fixture.module.ProbeError as error:
            caught = error
        self.assertIsNotNone(caught, "EXPECTED_FIXED_PROBE_ERROR")
        self.assertEqual(expected, caught.code)
        self.assertEqual(expected, str(caught))
        self.assertNotIn(DATA["private_marker"], str(caught))
        return caught

    def closed(self, fixture, client):
        self.assertIsNone(client.ws)
        self.assertIsNone(client.sock)
        self.assertTrue(fixture.sockets and all(sock.closed for sock in fixture.sockets))
        self.assertGreaterEqual(fixture.close_calls, 1)
        self.assertEqual(1, len(fixture.connects))
        self.assertEqual([], fixture.forbidden)
        self.assertEqual(0, fixture.graceful_calls)

    def _blocked_mutation_target(self, name, method, params):
        self.helper_invocations[name] = self.helper_invocations.get(name, 0) + 1
        with fixture_context(PRIMITIVES.response(1)) as fixture:
            client = fixture.open(read_only=True)
            observed_code = None
            try:
                client.call(method, params)
            except fixture.module.ProbeError as error:
                observed_code = error.code
            messages = decoded_messages(fixture)
            raw = fixture.sent_frames[0] if fixture.sent_frames else None
            witness = {"target": name, "observed_code": observed_code, "request_count": len(messages),
                "actual_request": messages[0] if messages else None, "expected_method": method,
                "expected_params": params, "next_id": client.next_id, "connects": len(fixture.connects),
                "request_wire_base64": base64.b64encode(raw).decode("ascii") if raw is not None else None,
                "request_wire_sha256": sha(raw) if raw is not None else None, "native_calls": 0}
            self.last_wrong_wire = witness
            TARGET_WITNESSES.append(witness)
            # Designated semantic failure: actual wrong request was captured first.
            self.assertEqual("RPC_PARAMS_FORBIDDEN", observed_code)
            self.assertEqual([], messages)
            self.assertEqual([], fixture.sent_frames)
            self.assertEqual(1, client.next_id)
            self.assertEqual(1, len(fixture.connects))
            self.helper_markers[name] = {"outcome": "PASS", "invocations": self.helper_invocations[name], "request_count": 0}

    def case_read_only_include_turns_true_never_sent(self):
        params = dict(DATA["read_params"], includeTurns=True)
        self._blocked_mutation_target("case_read_only_include_turns_true_never_sent", "thread/read", params)

    def case_read_only_limit_two_never_sent(self):
        params = dict(DATA["turns_params"], limit=2)
        self._blocked_mutation_target("case_read_only_limit_two_never_sent", "thread/turns/list", params)

    def test_read_only_metadata_wire_and_id_binding(self):
        metadata = {"thread": {"id": DATA["thread_id"], "privateMetadata": DATA["private_marker"]}, "extra": ["fixture"]}
        turns = {"data": [], "nextCursor": "synthetic-next-page-must-not-be-followed"}
        wire = PRIMITIVES.response(1) + PRIMITIVES.response(2, metadata) + PRIMITIVES.response(3, turns)
        with fixture_context(wire, chunk=3) as fixture:
            client = fixture.open(read_only=True)
            self.assertIs(client.read_only, True)
            self.assertEqual({}, client.call("initialize", DATA["initialize_params"]))
            client.notify_initialized()
            with mock.patch.object(client, "call", wraps=client.call) as call:
                actual = client.read_thread_metadata(DATA["thread_id"])
            self.assertEqual(1, call.call_count)
            self.assertEqual(mock.call("thread/read", DATA["read_params"]), call.call_args)
            self.assertTrue(actual == metadata, "FULL_DECODED_METADATA_MUST_BE_PRESERVED")
            self.assertEqual(3, len(decoded_messages(fixture)))
            received = client.call("thread/turns/list", DATA["turns_params"])
            self.assertTrue(received == turns, "DECODED_TURNS_MUST_BE_RETURNED")
            messages = decoded_messages(fixture)
            self.assertEqual([{"id": 1, "method": "initialize", "params": DATA["initialize_params"]},
                {"method": "initialized", "params": {}}, {"id": 2, "method": "thread/read", "params": DATA["read_params"]},
                {"id": 3, "method": "thread/turns/list", "params": DATA["turns_params"]}], messages)
            self.assertEqual(4, len(fixture.sent_frames))
            self.assertEqual({1, 2, 3}, client.completed_ids)
            self.assertEqual(4, client.next_id)
            self.assertEqual(1, len(fixture.connects))
            self.assertEqual(1, len(fixture.http_requests))
            self.assertEqual([], fixture.forbidden)
            self.assertEqual(LEGACY_METHODS, fixture.module.METHODS)
            summary(fixture, client, "EXPLICIT_INITIALIZE_HELPER_READ_TURNS_ONCE")
        for options in ({}, {"read_only": False}):
            with fixture_context() as fixture:
                client = fixture.open(**options)
                self.assertIs(client.read_only, False)
                self.assertEqual(LEGACY_METHODS, fixture.module.METHODS)
                self.error(fixture, lambda: client.call("thread/read", DATA["read_params"]), "RPC_METHOD_FORBIDDEN")
                self.error(fixture, lambda: client.call("thread/turns/list", DATA["turns_params"]), "RPC_METHOD_FORBIDDEN")
                self.assertEqual([], fixture.rpc_messages)
                self.assertEqual(1, client.next_id)

    def test_read_only_blocks_mutation_thread_and_pagination(self):
        self.case_read_only_include_turns_true_never_sent()
        self.case_read_only_limit_two_never_sent()
        methods = ("thread/resume", "thread/settings/update", "thread/queue/add", "thread/queue/list", "thread/start",
                   "account/rateLimits/read", "account/read", "config/read", "configRequirements/read", "turns/list", "initialized", "unknown")
        with fixture_context() as fixture:
            client = fixture.open(read_only=True)
            for method in methods:
                self.error(fixture, lambda method=method: client.call(method, DATA["read_params"]), "RPC_METHOD_FORBIDDEN")
            invalid = []
            for method, valid in (("thread/read", DATA["read_params"]), ("thread/turns/list", DATA["turns_params"])):
                invalid.extend((method, value) for value in (None, [], "not-a-dict"))
                invalid.append((method, dict(valid, threadId=DATA["other_thread_id"])))
                invalid.append((method, dict(valid, threadId=[])))
                invalid.append((method, dict(valid, extra=True)))
                for key in valid:
                    value = dict(valid); del value[key]
                    invalid.append((method, value))
            for value in (0, 1, "false", None):
                invalid.append(("thread/read", dict(DATA["read_params"], includeTurns=value)))
            for key, values in (("cursor", ("", "next")), ("limit", (True, 1.0, 0)),
                                ("itemsView", ("full", "notloaded", None)), ("sortDirection", ("asc", None))):
                invalid.extend(("thread/turns/list", dict(DATA["turns_params"], **{key: value})) for value in values)
            for method, params in invalid:
                before = client.next_id
                self.error(fixture, lambda method=method, params=params: client.call(method, params), "RPC_PARAMS_FORBIDDEN")
                self.assertEqual(before, client.next_id)
                self.assertEqual([], fixture.rpc_messages)
            summary(fixture, client, "ALL_FORBIDDEN_CALLS_SEND_ZERO")
        for mode, threads in ((None, (DATA["thread_id"],)), (1, (DATA["thread_id"],)),
                              (True, ()), (True, (DATA["thread_id"], DATA["other_thread_id"]))):
            with fixture_context() as fixture:
                self.error(fixture, lambda: fixture.module.UnixWebSocketRpc(DATA["endpoint"], threads, read_only=mode), "RPC_PARAMS_FORBIDDEN")
                self.assertEqual([], fixture.connects)
                self.assertEqual([], fixture.sockets)
                self.assertEqual([], fixture.rpc_messages)
        for options, thread, reason in (({"read_only": False}, DATA["thread_id"], "RPC_METHOD_FORBIDDEN"),
                                        ({"read_only": True}, DATA["other_thread_id"], "RPC_PARAMS_FORBIDDEN"),
                                        ({"read_only": True}, [], "RPC_PARAMS_FORBIDDEN")):
            with fixture_context() as fixture:
                client = fixture.open(**options)
                self.error(fixture, lambda: client.read_thread_metadata(thread), reason)
                self.closed(fixture, client)
                self.assertEqual([], fixture.rpc_messages)
                self.assertEqual(1, client.next_id)
        self.assertEqual({"case_read_only_include_turns_true_never_sent": 1, "case_read_only_limit_two_never_sent": 1}, self.helper_invocations)

    def test_read_only_wrong_response_id_is_not_accepted(self):
        for method, params in (("thread/read", DATA["read_params"]), ("thread/turns/list", DATA["turns_params"])):
            with fixture_context(PRIMITIVES.response(99)) as fixture:
                client = fixture.open(read_only=True)
                operation = (lambda: client.read_thread_metadata(DATA["thread_id"])) if method == "thread/read" else (lambda: client.call(method, params))
                self.error(fixture, operation, "RPC_RESPONSE_ID_MISMATCH")
                self.assertEqual([{"id": 1, "method": method, "params": params}], decoded_messages(fixture))
                self.assertEqual(set(), client.completed_ids)
                self.assertEqual(2, client.next_id)
                if method == "thread/read":
                    self.closed(fixture, client)
                else:
                    client.close(); self.closed(fixture, client)
                summary(fixture, client, "WRONG_RPC_ID_CLOSED_NO_REPLAY")
        for result, code in (({}, "RPC_THREAD_BINDING_MISMATCH"), ({"thread": None}, "RPC_THREAD_BINDING_MISMATCH"),
                             ({"thread": []}, "RPC_THREAD_BINDING_MISMATCH"), ({"thread": {}}, "RPC_THREAD_BINDING_MISMATCH"),
                             ({"thread": {"id": DATA["other_thread_id"]}}, "RPC_THREAD_BINDING_MISMATCH")):
            with fixture_context(PRIMITIVES.response(1, result)) as fixture:
                client = fixture.open(read_only=True)
                self.error(fixture, lambda: client.read_thread_metadata(DATA["thread_id"]), code)
                self.closed(fixture, client)
                self.assertEqual([{"id": 1, "method": "thread/read", "params": DATA["read_params"]}], decoded_messages(fixture))
                summary(fixture, client, "RPC_ID_MATCHED_THREAD_BINDING_REJECTED")
        for kind in ("ordinary", "probe"):
            with fixture_context() as fixture:
                client = fixture.open(read_only=True)
                injected = RuntimeError(DATA["private_marker"]) if kind == "ordinary" else fixture.module.ProbeError("RPC_REMOTE_ERROR")
                with mock.patch.object(client, "call", side_effect=injected) as call:
                    caught = self.error(fixture, lambda: client.read_thread_metadata(DATA["thread_id"]),
                                        "RPC_METADATA_UNCONFIRMED" if kind == "ordinary" else "RPC_REMOTE_ERROR")
                self.assertEqual(1, call.call_count)
                self.assertEqual(mock.call("thread/read", DATA["read_params"]), call.call_args)
                if kind == "probe": self.assertIs(injected, caught)
                self.closed(fixture, client)
                self.assertEqual([], fixture.rpc_messages)
                summary(fixture, client, "HELPER_CALL_ERROR_CLOSED_FIXED_CODE")

    def test_read_only_deadlines_and_loss_never_replay(self):
        for method, params in (("thread/read", DATA["read_params"]), ("thread/turns/list", DATA["turns_params"])):
            for total, request in ((60, 1), (1, 5)):
                with fixture_context(PRIMITIVES.response(1)) as fixture:
                    client = fixture.open(read_only=True, total_timeout=total, request_timeout=request)
                    actual_send = client.ws.send

                    def expire_after_actual_send(raw):
                        sent = actual_send(raw)
                        fixture.clock = min(client.deadline, client.request_deadline)
                        return sent

                    with mock.patch.object(client.ws, "send", side_effect=expire_after_actual_send):
                        operation = (lambda: client.read_thread_metadata(DATA["thread_id"])) if method == "thread/read" else (lambda: client.call(method, params))
                        self.error(fixture, operation, "RPC_REQUEST_TIMEOUT")
                    self.assertEqual([{"id": 1, "method": method, "params": params}], decoded_messages(fixture))
                    self.assertEqual(2, client.next_id)
                    self.assertTrue(fixture.timeout_values and 0 < fixture.timeout_values[-1] <= min(total, request))
                    if method != "thread/read": client.close()
                    self.closed(fixture, client)
                    summary(fixture, client, "ACTUAL_SEND_THEN_DEADLINE_NO_REPLAY")
        for failure, code, sent in (("recv", "RPC_RECEIVE_FAILED", 1), ("send", "RPC_SEND_FAILED", 0)):
            with fixture_context(PRIMITIVES.response(1), failure=failure) as fixture:
                client = fixture.open(read_only=True)
                self.error(fixture, lambda: client.read_thread_metadata(DATA["thread_id"]), code)
                self.closed(fixture, client)
                self.assertEqual(sent, len(decoded_messages(fixture)))
                self.assertEqual(2, client.next_id)
                summary(fixture, client, "LOSS_UNKNOWN_NO_RETRY_OR_FOLLOWUP")
        with fixture_context() as fixture:
            client = fixture.open(read_only=True, total_timeout=1)
            fixture.clock = client.deadline
            self.error(fixture, lambda: client.read_thread_metadata(DATA["thread_id"]), "RPC_REQUEST_TIMEOUT")
            self.closed(fixture, client)
            self.assertEqual([], fixture.rpc_messages)
            summary(fixture, client, "TOTAL_EXPIRED_BEFORE_SEND")
