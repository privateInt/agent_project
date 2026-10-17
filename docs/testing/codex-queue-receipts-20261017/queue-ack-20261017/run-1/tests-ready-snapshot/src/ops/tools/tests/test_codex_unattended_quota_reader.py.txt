"""Frozen owner-binding 4ef0459d + D06: fake-only quota checks (three methods)."""
import contextlib
import copy
import json
import os
from pathlib import Path
import threading
import unittest
from unittest import mock

from src.ops.tools import codex_unattended_quota_reader as product
from src.ops.tools.tests import test_codex_unattended_intent_journal as support

FIXTURES = Path(__file__).resolve().parent / "fixtures/codex_unattended_owner_binding"
SCHEMA_PINS = {
    "GetAccountRateLimitsResponse.json": "cb9655e68130116f634ed9353e45b336492e6069a6628f424d1cd52e54eda0e0",
    "InitializeParams.json": "b55124e7c0f36a1789396daa5578217a92be67f053903696236686e59f6f212e",
    "ThreadQueueAddParams.json": "60f25b7d3e3357c215bef9d2fe7047200f545e030dca9e00527ac0620cd0c88f",
}
REPORT_KEYS = {
    "scope", "status", "phase", "reason", "error_code", "requests",
    "initialized_call_entered", "initialized_notification_completed", "retries",
    "ordinaryUsageAllowed", "ordinary_usage_observed_allowed", "windows",
    "transport_pin_verified", "wire_send_verified", "full_quota_admission_verified",
    "direct_external_io_performed", "thread_operations", "settings_changed",
    "queue_changed", "services_changed", "remote_mutation_authorized",
    "native_acceptance_verified", "replay_permitted", "native_ready",
}
FALSE_FLAGS = {
    "transport_pin_verified", "wire_send_verified", "full_quota_admission_verified",
    "direct_external_io_performed", "settings_changed", "queue_changed", "services_changed",
    "remote_mutation_authorized", "native_acceptance_verified", "replay_permitted", "native_ready",
}
EXTERNAL_CODES = set("""RPC_REQUEST_TIMEOUT RPC_WIRE_LIMIT RPC_LINE_LIMIT RPC_STDOUT_LIMIT
RPC_CONNECT_FAILED RPC_DEPENDENCY_UNSUPPORTED RPC_UPGRADE_REJECTED RPC_SEND_FAILED
RPC_RECEIVE_FAILED RPC_REMOTE_ERROR RPC_RESPONSE_INVALID RPC_TEXT_REQUIRED RPC_NONFINITE_JSON
RPC_SERVER_REQUEST_FORBIDDEN RPC_NOTIFICATION_LIMIT RPC_DUPLICATE_RESPONSE RPC_RESPONSE_ID_MISMATCH
RPC_RESULT_INVALID RPC_CLIENT_CLOSED RPC_METHOD_FORBIDDEN RPC_PARAMS_FORBIDDEN RPC_REQUEST_TOO_LARGE""".split())
LOCAL_CODES = {"CLIENT_INTERFACE_INVALID", "INITIALIZE_RESULT_INVALID", "QUOTA_RESULT_INVALID",
               "UNCLASSIFIED_LOCAL_OR_TRANSPORT_ERROR"}
INIT_PARAMS = {"clientInfo": {"name": "agent_project_quota_reader", "version": "1"},
               "capabilities": {"experimentalApi": True}}
OBSERVATIONS = []


def fixture(name):
    manifest = json.loads((FIXTURES / "manifest.json").read_bytes())
    if not manifest["fixture_only"] or not manifest["schema_reference_not_framework_fork"]:
        raise AssertionError("SYNTHETIC_FIXTURE_REQUIRED")
    for filename, pin in manifest["files"].items():
        if support.sha((FIXTURES / filename).read_bytes()) != pin:
            raise AssertionError("FIXTURE_PIN_MISMATCH")
    for filename, pin in SCHEMA_PINS.items():
        if manifest["files"][filename] != pin:
            raise AssertionError("GENERATED_SCHEMA_PIN_MISMATCH")
    return json.loads((FIXTURES / name).read_bytes())


@contextlib.contextmanager
def strict_io():
    """Reuse PR18 guard; BaseException additionally protects environment/thread boundaries."""
    with support.no_external_io() as observed:
        attempts, output = observed

        def denied(*args, **kwargs):
            attempts.append("FORBIDDEN_EXTERNAL_IO")
            raise support.ForbiddenIO("OWNER_BINDING_EXTERNAL_IO_FORBIDDEN")

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(threading.Thread, "start", denied))
            for name in ("__getitem__", "__setitem__", "__delitem__", "__iter__", "__len__"):
                stack.enter_context(mock.patch.object(type(os.environ), name, denied))
            for name in ("putenv", "unsetenv", "fork", "posix_spawn", "posix_spawnp"):
                if hasattr(os, name):
                    stack.enter_context(mock.patch.object(os, name, denied))
            yield attempts, output


class Opaque:
    """Ignoring unknown private metadata must not inspect or stringify it."""
    def __str__(self):
        raise support.ForbiddenIO("PRIVATE_VALUE_STRINGIFIED")

    __repr__ = __str__

    def __deepcopy__(self, memo):
        raise support.ForbiddenIO("PRIVATE_RESPONSE_COPIED")

    def __bool__(self):
        raise support.ForbiddenIO("PRIVATE_VALUE_COERCED")

    def __eq__(self, other):
        raise support.ForbiddenIO("HOSTILE_EQUALITY_CALLED")

    __hash__ = None


class HostileCode(str):
    def __eq__(self, other):
        raise support.ForbiddenIO("HOSTILE_CODE_EQUALITY_CALLED")

    __hash__ = str.__hash__


class ClientFailure(Exception):
    def __init__(self, code):
        super().__init__("PRIVATE_SYNTHETIC_CLIENT_EXCEPTION_NEVER_LOG")
        self.code = code

    def __str__(self):
        raise support.ForbiddenIO("EXCEPTION_STRINGIFIED")

    __repr__ = __str__


class PropertyFailure(Exception):
    @property
    def code(self):
        raise support.ForbiddenIO("EXCEPTION_CODE_PROPERTY_READ")


class FakeClient:
    def __init__(self, quota, initialize_response, *, error_at=None, error=None):
        self.quota, self.initialize_response = quota, initialize_response
        self.error_at, self.error = error_at, error
        self.trace, self.completed = [], []
        self.send_count = 0
        self.context_entries = self.context_exits = self.closes = 0

    def call(self, method, params):
        if method not in ("initialize", "account/rateLimits/read"):
            raise support.ForbiddenIO("NON_QUOTA_RPC_FORBIDDEN")
        expected = INIT_PARAMS if method == "initialize" else {}
        if params != expected:
            raise AssertionError("EXACT_RPC_PARAMS_REQUIRED")
        self.trace.append((method, copy.deepcopy(params)))
        if self.error_at == method:
            raise self.error
        self.completed.append(method)
        return self.initialize_response if method == "initialize" else self.quota

    def notify_initialized(self):
        self.trace.append(("initialized", None))
        if self.error_at == "initialized":
            raise self.error
        self.completed.append("initialized")

    def close(self):
        self.closes += 1
        raise support.ForbiddenIO("BORROWED_CLIENT_CLOSE_FORBIDDEN")

    def __enter__(self):
        self.context_entries += 1
        raise support.ForbiddenIO("BORROWED_CLIENT_CONTEXT_FORBIDDEN")

    def __exit__(self, *args):
        self.context_exits += 1
        raise support.ForbiddenIO("BORROWED_CLIENT_CONTEXT_FORBIDDEN")


class QuotaReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = fixture("quota.json")
        if cls.data["provenance"] != "SYNTHETIC_ONLY_FAKE_CLIENT_NO_NATIVE_IO":
            raise AssertionError("SYNTHETIC_FIXTURE_REQUIRED")

    def snapshot(self, ordinary=True, *, percent=14):
        window = dict(self.data["window"], usedPercent=percent)
        bucket = {"limitId": self.data["limit_id"], "primary": window,
                  "secondary": None, "rateLimitReachedType": None,
                  "accountId": self.data["ignored_secret"], "credits": Opaque()}
        return {"ordinaryUsageAllowed": ordinary, "rateLimits": bucket,
                "rawMetadata": Opaque(), "account": self.data["ignored_secret"]}

    def client(self, quota, **options):
        return FakeClient(quota, dict(self.data["initialize_response"]), **options)

    def invoke(self, client, *, reason, phase="COMPLETE", error=None):
        with strict_io() as (attempts, output):
            result = product.read_quota_only(client)
        self.last_result = result
        self.assertEqual([], attempts)
        self.assertEqual("", output.getvalue())
        self.assertIs(type(result), dict)
        self.assertEqual(REPORT_KEYS, set(result))
        self.assertEqual("QUOTA_ONLY_READER", result["scope"])
        self.assertEqual(0, result["retries"])
        self.assertIs(type(result["retries"]), int)
        self.assertEqual(0, result["thread_operations"])
        for flag in FALSE_FLAGS:
            self.assertIs(result[flag], False)
        for flag in ("initialized_call_entered", "initialized_notification_completed",
                     "ordinary_usage_observed_allowed"):
            self.assertIs(type(result[flag]), bool)
        self.assertTrue(result["ordinaryUsageAllowed"] is None or
                        type(result["ordinaryUsageAllowed"]) is bool)
        self.assertIs(type(result["requests"]), list)
        self.assertLessEqual(len(result["requests"]), 2)
        self.assertIs(type(result["windows"]), list)
        self.assertLessEqual(len(result["windows"]), 16)
        for window in result["windows"]:
            self.assertIs(type(window), dict)
            self.assertEqual({"limit_id_sha256", "reached_type_is_null", "primary", "secondary"}, set(window))
            self.assertTrue(window["limit_id_sha256"] is None or
                            (type(window["limit_id_sha256"]) is str and len(window["limit_id_sha256"]) == 64))
            self.assertTrue(window["reached_type_is_null"] is None or type(window["reached_type_is_null"]) is bool)
            for name in ("primary", "secondary"):
                if window[name] is not None:
                    self.assertIs(type(window[name]), dict)
                    self.assertEqual({"usedPercent", "windowDurationMins", "resetsAt"}, set(window[name]))
                    self.assertIs(type(window[name]["usedPercent"]), int)
                    self.assertTrue(0 <= window[name]["usedPercent"] <= 100)
                    for field in ("windowDurationMins", "resetsAt"):
                        self.assertTrue(window[name][field] is None or type(window[name][field]) is int)
        if isinstance(client, FakeClient):
            entered = [m for m, _ in client.trace if m != "initialized"]
            self.assertEqual([{"method": m, "call_entered": True,
                               "response_observed": m in client.completed} for m in entered], result["requests"])
            self.assertIs(result["initialized_call_entered"], any(m == "initialized" for m, _ in client.trace))
            self.assertIs(result["initialized_notification_completed"], "initialized" in client.completed)
            self.assertEqual((0, 0, 0, 0), (client.send_count, client.context_entries, client.context_exits, client.closes))
        for request in result["requests"]:
            self.assertIs(type(request), dict)
            self.assertEqual({"method", "call_entered", "response_observed"}, set(request))
            self.assertIs(type(request["call_entered"]), bool)
            self.assertIs(type(request["response_observed"]), bool)
        self.assertTrue(result["error_code"] is None or
                        (type(result["error_code"]) is str and result["error_code"] in EXTERNAL_CODES | LOCAL_CODES))
        encoded = support.canonical(result)
        for private in (self.data["limit_id"], self.data["ignored_secret"],
                        *self.data["initialize_response"].values(), "PRIVATE_SYNTHETIC_CLIENT_EXCEPTION_NEVER_LOG"):
            if private not in ("unix", "linux"):
                self.assertNotIn(private.encode(), encoded)
        OBSERVATIONS.append({"reason": result["reason"], "phase": result["phase"],
                             "requests": len(result["requests"]), "runtime_calls": 0})
        self.assertEqual(phase, result["phase"])
        self.assertEqual(error, result["error_code"])
        expected_status = {"ORDINARY_ALLOWED_OBSERVED": "OBSERVED_ALLOWED",
                           "ORDINARY_BLOCKED_OBSERVED": "OBSERVED_BLOCKED",
                           "ORDINARY_USAGE_UNKNOWN": "OBSERVED_UNKNOWN",
                           "QUOTA_SNAPSHOT_INVALID": "OBSERVED_UNKNOWN"}.get(reason, "UNCONFIRMED")
        self.assertEqual(expected_status, result["status"])
        self.assertEqual(reason, result["reason"])
        return result

    def test_quota_sequence_sanitization_and_fail_closed(self):
        for ordinary, percent, reason in ((True, 14, "ORDINARY_ALLOWED_OBSERVED"),
                                         (False, 100, "ORDINARY_BLOCKED_OBSERVED"),
                                         (True, 100, "ORDINARY_ALLOWED_OBSERVED")):
            with self.subTest(ordinary=ordinary, percent=percent):
                client = self.client(self.snapshot(ordinary, percent=percent))
                result = self.invoke(client, reason=reason)
                self.assertEqual([("initialize", INIT_PARAMS), ("initialized", None),
                                  ("account/rateLimits/read", {})], client.trace)
                self.assertIs(result["ordinaryUsageAllowed"], ordinary)
                self.assertIs(result["ordinary_usage_observed_allowed"], ordinary)
                self.assertEqual([{"limit_id_sha256": support.sha(self.data["limit_id"].encode()),
                                   "reached_type_is_null": True,
                                   "primary": dict(self.data["window"], usedPercent=percent), "secondary": None}],
                                 result["windows"])
        for ordinary in (1, 0, "true", [], Opaque()):
            result = self.invoke(self.client(self.snapshot(ordinary)), reason="ORDINARY_USAGE_UNKNOWN")
            self.assertIsNone(result["ordinaryUsageAllowed"])
            self.assertIs(result["ordinary_usage_observed_allowed"], False)
        absent = self.snapshot()
        del absent["ordinaryUsageAllowed"]
        self.assertIsNone(self.invoke(self.client(absent), reason="ORDINARY_USAGE_UNKNOWN")["ordinaryUsageAllowed"])
        for count in (1, 16):
            snapshot = self.snapshot()
            snapshot["rateLimitsByLimitId"] = {"bucket-%02d" % n: copy.copy(snapshot["rateLimits"]) for n in range(count)}
            result = self.invoke(self.client(snapshot), reason="ORDINARY_ALLOWED_OBSERVED")
            self.assertEqual({support.sha(k.encode()) for k in snapshot["rateLimitsByLimitId"]},
                             {v["limit_id_sha256"] for v in result["windows"]})
        for reached, expected in (("rate_limit_reached", False),
                                  ("workspace_owner_credits_depleted", False),
                                  ("workspace_member_credits_depleted", False),
                                  ("workspace_owner_usage_limit_reached", False),
                                  ("workspace_member_usage_limit_reached", False), (None, True)):
            snapshot = self.snapshot()
            snapshot["rateLimits"]["rateLimitReachedType"] = reached
            self.assertIs(self.invoke(self.client(snapshot), reason="ORDINARY_ALLOWED_OBSERVED")["windows"][0]["reached_type_is_null"], expected)
        snapshot = self.snapshot()
        for field in ("limitId", "rateLimitReachedType"):
            del snapshot["rateLimits"][field]
        snapshot["rateLimits"]["primary"] = {"usedPercent": 0}
        projected = self.invoke(self.client(snapshot), reason="ORDINARY_ALLOWED_OBSERVED")["windows"][0]
        self.assertEqual({"limit_id_sha256": None, "reached_type_is_null": None,
                          "primary": {"usedPercent": 0, "windowDurationMins": None, "resetsAt": None},
                          "secondary": None}, projected)
        secondary = self.snapshot()
        secondary["rateLimits"]["secondary"] = secondary["rateLimits"].pop("primary")
        secondary["rateLimitsByLimitId"] = None
        result = self.invoke(self.client(secondary), reason="ORDINARY_ALLOWED_OBSERVED")
        self.assertIsNone(result["windows"][0]["primary"])
        self.assertEqual(self.data["window"], result["windows"][0]["secondary"])
        for limit_id in (None, "é"*128):
            snapshot = self.snapshot()
            snapshot["rateLimits"]["limitId"] = limit_id
            result = self.invoke(self.client(snapshot), reason="ORDINARY_ALLOWED_OBSERVED")
            self.assertEqual(None if limit_id is None else support.sha(limit_id.encode()), result["windows"][0]["limit_id_sha256"])
        for duration, reset in ((1, 0), (2**63-1, 2**63-1), (None, None)):
            snapshot = self.snapshot()
            snapshot["rateLimits"]["primary"].update(windowDurationMins=duration, resetsAt=reset)
            self.invoke(self.client(snapshot), reason="ORDINARY_ALLOWED_OBSERVED")
        invalid = []
        for field, values in (("usedPercent", (True, False, -1, 101, 1.0, float("nan"), float("inf"), 2**80, None)),
                              ("windowDurationMins", (True, 0, -1, 1.0, 2**63)),
                              ("resetsAt", (True, -1, 1.0, 2**63))):
            for value in values:
                snapshot = self.snapshot()
                snapshot["rateLimits"]["primary"][field] = value
                invalid.append(snapshot)
        for value in ({}, [], "bad", None):
            snapshot = self.snapshot()
            snapshot["rateLimits"] = value
            invalid.append(snapshot)
        for value in ({}, {str(n): self.snapshot()["rateLimits"] for n in range(17)}, [], 1):
            snapshot = self.snapshot()
            snapshot["rateLimitsByLimitId"] = value
            invalid.append(snapshot)
        for value in ("", "x"*257, "é"*129, "\ud800", 1):
            snapshot = self.snapshot()
            snapshot["rateLimitsByLimitId"] = {value: self.snapshot()["rateLimits"]}
            invalid.append(snapshot)
        for change in ({"primary": None, "secondary": None}, {"primary": {}},
                       {"primary": []}, {"rateLimitReachedType": "future_unknown"},
                       {"rateLimitReachedType": True}, {"limitId": 123}):
            snapshot = self.snapshot()
            snapshot["rateLimits"].update(change)
            invalid.append(snapshot)
        invalid.extend(({}, [], None))
        for index, snapshot in enumerate(invalid):
            with self.subTest(invalid_snapshot=index):
                result = self.invoke(self.client(snapshot), reason="QUOTA_SNAPSHOT_INVALID",
                                     phase="SANITIZE", error="QUOTA_RESULT_INVALID")
                self.assertIs(result["ordinary_usage_observed_allowed"], False)
                self.assertEqual([], result["windows"])
                self.assertIs(result["ordinaryUsageAllowed"], True if type(snapshot) is dict and snapshot.get("ordinaryUsageAllowed") is True else None)

    def test_quota_fault_phases_and_no_send_inference(self):
        for invalid in (None, object(), {"call": None}):
            result = self.invoke(invalid, reason="CLIENT_INTERFACE_INVALID", phase="PRECHECK", error="CLIENT_INTERFACE_INVALID")
            self.assertEqual([], result["requests"])
        for value in (None, [], "private", 0):
            client = self.client(self.snapshot())
            client.initialize_response = value
            result = self.invoke(client, reason="INITIALIZE_RESULT_INVALID", phase="INITIALIZE", error="INITIALIZE_RESULT_INVALID")
            self.assertEqual([("initialize", INIT_PARAMS)], client.trace)
            self.assertEqual([], result["windows"])
        for point, phase, trace in (("initialize", "INITIALIZE", ["initialize"]),
                                    ("initialized", "INITIALIZED_NOTIFICATION", ["initialize", "initialized"]),
                                    ("account/rateLimits/read", "RATE_LIMITS_READ", ["initialize", "initialized", "account/rateLimits/read"])):
            for code in sorted(EXTERNAL_CODES):
                client = self.client(self.snapshot(), error_at=point, error=ClientFailure(code))
                result = self.invoke(client, reason="CLIENT_CALL_UNCONFIRMED", phase=phase, error=code)
                self.assertEqual(trace, [m for m, _ in client.trace])
                self.assertEqual([], result["windows"])
                self.assertIsNone(result["ordinaryUsageAllowed"])
                self.assertIs(result["ordinary_usage_observed_allowed"], False)
        for error in (ClientFailure("PRIVATE_UNKNOWN_CODE"), ClientFailure(1), ClientFailure(Opaque()),
                      ClientFailure(HostileCode("RPC_REQUEST_TIMEOUT")),
                      PropertyFailure("PRIVATE_PROPERTY_EXCEPTION")):
            client = self.client(self.snapshot(), error_at="account/rateLimits/read", error=error)
            self.invoke(client, reason="CLIENT_CALL_UNCONFIRMED", phase="RATE_LIMITS_READ",
                        error="UNCLASSIFIED_LOCAL_OR_TRANSPORT_ERROR")
            self.assertEqual(0, client.send_count)

    def test_unknown_ordinary_never_allowed(self):
        # M_Q: only null changes; every selected quota field and fake call is valid.
        result = self.invoke(self.client(self.snapshot(None)), reason="ORDINARY_USAGE_UNKNOWN")
        self.assertIsNone(result["ordinaryUsageAllowed"])
        self.assertIs(result["ordinary_usage_observed_allowed"], False)
        self.assertEqual(self.data["window"], result["windows"][0]["primary"])
