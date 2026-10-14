"""D29 v2 준비 기준: 합성 stdio/계정 메타데이터, native·추론 인수 아님."""

import copy
import fcntl
import importlib.util
import io
import json
import os
import signal
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[4]
SPEC = importlib.util.spec_from_file_location(
    "codex_readiness_under_test", ROOT / "src/ops/tools/codex_readiness.py"
)
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)

SECRET = "CANARY-NOT-A-REAL-TOKEN-readiness-20261014"
EMAIL = "canary-readiness@example.invalid"
ACCOUNT_ID = "canary-account-id-do-not-publish"
CURSOR = "opaque-cursor-do-not-publish"
RESET_AT = 2000000000

CONFIG = {
    "config": {
        "model": "gpt-6.1-sol",
        "model_reasoning_effort": "xhigh",
        "forced_login_method": "chatgpt",
        "agents": {
            "enabled": False,
            "max_concurrent_threads_per_session": 2,
            "default_subagent_model": "gpt-6.1-sol",
            "default_subagent_reasoning_effort": "xhigh",
        },
        "unknown_private_field": SECRET,
    }
}
ACCOUNT = {
    "account": {"type": "chatgpt", "email": EMAIL, "id": ACCOUNT_ID},
    "requiresOpenaiAuth": True,
    "unknown_private_field": SECRET,
}
TARGET_MODEL = {
    "id": "fixture-catalog-id",
    "model": "gpt-6.1-sol",
    "supportedReasoningEfforts": [{"reasoningEffort": "xhigh"}],
    "unknown_private_field": SECRET,
}
MODELS = {"data": [TARGET_MODEL], "nextCursor": None}
WINDOW = {"usedPercent": 20, "windowDurationMins": 300, "resetsAt": RESET_AT}
QUOTA = {
    "primary": WINDOW,
    "secondary": dict(WINDOW, usedPercent=10, windowDurationMins=10080),
    "rateLimitReachedType": None,
    "credits": {"hasCredits": True, "unlimited": False, "balance": "1.00",
                "unknown_private_field": SECRET},
}
LIMITS = {"rateLimits": QUOTA}

# 하나의 fixture 프로세스만 사용한다. repo/인증 파일이나 모델에 접근하지 않는다.
FAKE_SERVER = r'''
import json, os, signal, subprocess, sys, time
if len(sys.argv) > 1 and sys.argv[1] == "--version":
    print("codex-cli 0.0.0")
    sys.exit(0)
scenario = json.loads(sys.argv[1])
mode = scenario.get("mode", "normal")
if scenario.get("auth_write"):
    with open(scenario["auth_write"], "w") as stream:
        stream.write(scenario["secret"])
if scenario.get("pid"):
    with open(scenario["pid"], "w") as stream:
        stream.write(str(os.getpid()))
if mode in ("group_parent_exited", "group_parent_term"):
    child_code = (
        "import json,os,signal,sys,time\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "state=json.loads(sys.argv[1])\n"
        "with open(state['pid'],'w') as stream: stream.write(str(os.getpid()))\n"
        "tick=0\n"
        "while True:\n"
        "    with open(state['heartbeat']+'.next','w') as stream: stream.write(str(tick))\n"
        "    os.replace(state['heartbeat']+'.next',state['heartbeat'])\n"
        "    tick+=1\n"
        "    time.sleep(0.01)\n"
    )
    subprocess.Popen([sys.executable, "-u", "-c", child_code,
                      json.dumps({"pid":scenario["child_pid"],"heartbeat":scenario["heartbeat"]})])
    ready_until = time.monotonic() + 1
    while not os.path.exists(scenario["child_pid"]) and time.monotonic() < ready_until:
        time.sleep(0.005)
    if mode == "group_parent_exited":
        os._exit(0)
if mode == "ignore_term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
def emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)
initialized = False
page = 0
for raw in sys.stdin:
    request = json.loads(raw)
    if scenario.get("log"):
        with open(scenario["log"], "a") as stream:
            stream.write(json.dumps(request) + "\n")
    method = request.get("method")
    if method == "initialized":
        initialized = True
        continue
    request_id = request.get("id")
    if method == "initialize":
        if scenario.get("initialize_error"):
            emit({"jsonrpc":"2.0", "id":request_id,
                  "error":{"code":-32000, "message":scenario["secret"]}})
            continue
        emit({"jsonrpc":"2.0", "id":request_id,
              "result":{"userAgent":"fixture", "platformFamily":"fixture", "platformOs":"fixture"}})
        continue
    if not initialized:
        emit({"jsonrpc":"2.0", "id":request_id,
              "error":{"code":-32000,"message":"initialized notification required"}})
        continue
    if mode in ("timeout", "ignore_term"):
        time.sleep(3600)
    if mode == "slow_read":
        time.sleep(scenario.get("delay", 0.1))
    if mode == "malformed":
        sys.stdout.write("{malformed-json\n"); sys.stdout.flush(); continue
    if mode == "invalid_utf8":
        sys.stdout.buffer.write(b"\xff\n"); sys.stdout.buffer.flush(); continue
    if mode == "stdout_line":
        sys.stdout.write("x" * (128*1024 + 1) + "\n"); sys.stdout.flush(); continue
    if mode == "stdout_total":
        for index in range(10):
            emit({"jsonrpc":"2.0", "method":"account/rateLimits/updated",
                  "params":{"padding":"x" * (60*1024)}})
    if mode == "stderr_total":
        sys.stderr.write(scenario["secret"] + "x" * (64*1024 + 1)); sys.stderr.flush()
    if mode == "notifications":
        for index in range(33):
            emit({"jsonrpc":"2.0", "method":"account/rateLimits/updated", "params":{}})
    if mode == "server_request":
        emit({"jsonrpc":"2.0", "id":"server-request",
              "method":"fixture/unknown", "params":{"secret":scenario["secret"]}})
        continue
    if mode == "rpc_error":
        emit({"jsonrpc":"2.0", "id":request_id,
              "error":{"code":-32000,"message":scenario["secret"],
                       "data":{"private":scenario["secret"]}}})
        continue
    if method == "config/read":
        result = scenario["config"]
    elif method == "account/read":
        result = scenario["account"]
    elif method == "model/list":
        pages = scenario["pages"]
        result = pages[min(page, len(pages)-1)]
        page += 1
    elif method == "account/rateLimits/read":
        result = scenario["limits"]
    else:
        emit({"jsonrpc":"2.0", "id":request_id,
              "error":{"code":-32601,"message":"unexpected method"}})
        continue
    if mode == "wrong_id":
        request_id = "not-the-issued-id"
    response = {"jsonrpc":"2.0", "id":request_id, "result":result}
    emit(response)
    if mode == "stdin_stall" and method == "model/list":
        # 첫 cursor 응답 이후 다음 요청을 읽지 않아 실제 pipe backpressure를 만든다.
        time.sleep(3600)
    if mode == "duplicate_id":
        emit(response)
'''


def scenario(**changes):
    result = {
        "config": copy.deepcopy(CONFIG), "account": copy.deepcopy(ACCOUNT),
        "pages": [copy.deepcopy(MODELS)], "limits": copy.deepcopy(LIMITS),
        "secret": SECRET,
    }
    result.update(changes)
    return result


class ReadinessAssertions(unittest.TestCase):
    def assert_status(self, value, status, reason=None):
        self.assertIsInstance(value, dict)
        self.assertEqual(value.get("status"), status)
        if reason is not None:
            self.assertEqual(value.get("reason"), reason)

    def assert_public(self, value):
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        for private in (SECRET, EMAIL, ACCOUNT_ID, CURSOR):
            self.assertNotIn(private, encoded)


class ContractTests(ReadinessAssertions):

    def test_config_known_policy_is_a_scoped_pass(self):
        self.assert_status(m.evaluate_config(copy.deepcopy(CONFIG)), "PASS")

    def test_config_missing_fields_stay_unknown(self):
        for value in ({}, {"config": None}, {"config": []}, {"config": {}}, None):
            with self.subTest(value=value):
                self.assert_status(m.evaluate_config(value), "BLOCKED", "CONFIG_FIELDS_UNKNOWN")
        for key in ("model", "model_reasoning_effort", "forced_login_method", "agents"):
            value = copy.deepcopy(CONFIG)
            del value["config"][key]
            with self.subTest(missing=key):
                self.assert_status(m.evaluate_config(value), "BLOCKED", "CONFIG_FIELDS_UNKNOWN")
        for key in CONFIG["config"]["agents"]:
            value = copy.deepcopy(CONFIG)
            del value["config"]["agents"][key]
            with self.subTest(missing_agent_key=key):
                self.assert_status(m.evaluate_config(value), "BLOCKED", "CONFIG_FIELDS_UNKNOWN")

    def test_config_explicit_policy_conflicts_fail(self):
        conflicts = (
            ("model", "other-model"), ("model_reasoning_effort", "high"),
            ("forced_login_method", "api"),
        )
        for key, wrong in conflicts:
            value = copy.deepcopy(CONFIG)
            value["config"][key] = wrong
            with self.subTest(key=key):
                self.assert_status(m.evaluate_config(value), "FAIL", "CONFIG_POLICY_MISMATCH")
        for key, wrong in (("enabled", True), ("max_concurrent_threads_per_session", 3),
                           ("default_subagent_model", "other-model"),
                           ("default_subagent_reasoning_effort", "high")):
            value = copy.deepcopy(CONFIG)
            value["config"]["agents"][key] = wrong
            with self.subTest(agent_key=key):
                self.assert_status(m.evaluate_config(value), "FAIL", "CONFIG_POLICY_MISMATCH")

    def test_config_malformed_values_do_not_prove_policy(self):
        for key, wrong in (("model", None), ("model_reasoning_effort", []),
                           ("forced_login_method", {}), ("agents", "unknown")):
            value = copy.deepcopy(CONFIG)
            value["config"][key] = wrong
            with self.subTest(key=key):
                self.assert_status(m.evaluate_config(value), "BLOCKED")
        value = copy.deepcopy(CONFIG)
        value["config"]["agents"]["max_concurrent_threads_per_session"] = True
        self.assert_status(m.evaluate_config(value), "BLOCKED")

    def test_account_chatgpt_requires_openai_auth(self):
        self.assert_status(m.evaluate_account(copy.deepcopy(ACCOUNT)), "PASS")
        value = copy.deepcopy(ACCOUNT)
        value["requiresOpenaiAuth"] = False
        self.assert_status(m.evaluate_account(value), "FAIL", "ACCOUNT_PROVIDER_MISMATCH")

    def test_account_other_authentication_is_not_a_fallback(self):
        for auth_type in ("apiKey", "chatgptAuthTokens", "external-token"):
            value = copy.deepcopy(ACCOUNT)
            value["account"]["type"] = auth_type
            with self.subTest(auth_type=auth_type):
                self.assert_status(m.evaluate_account(value), "FAIL", "ACCOUNT_AUTH_METHOD_MISMATCH")

    def test_account_missing_or_unusable_auth_is_blocked(self):
        values = ({}, None, {"account": None, "requiresOpenaiAuth": True},
                  {"account": {}, "requiresOpenaiAuth": True},
                  {"account": {"type": "chatgpt"}},
                  {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": "true"})
        for value in values:
            with self.subTest(value=value):
                self.assert_status(m.evaluate_account(value), "BLOCKED", "ACCOUNT_UNKNOWN")

    def test_model_catalog_walks_all_pages(self):
        pages = [{"data": [{"id": "unrelated", "model": "other"}], "nextCursor": CURSOR},
                 copy.deepcopy(MODELS)]
        self.assert_status(m.evaluate_models(pages), "PASS")

    def test_model_id_alias_can_identify_requested_model(self):
        model = copy.deepcopy(TARGET_MODEL)
        model["id"] = "gpt-6.1-sol"
        del model["model"]
        self.assert_status(m.evaluate_models([{"data": [model], "nextCursor": None}]), "PASS")

    def test_model_absence_and_effort_mismatch_are_explicit_failure(self):
        self.assert_status(m.evaluate_models([{"data": [], "nextCursor": None}]),
                           "FAIL", "MODEL_UNAVAILABLE")
        value = copy.deepcopy(MODELS)
        value["data"][0]["supportedReasoningEfforts"] = [{"reasoningEffort": "high"}]
        self.assert_status(m.evaluate_models([value]), "FAIL", "MODEL_EFFORT_UNAVAILABLE")

    def test_model_unknown_effort_and_malformed_catalog_stay_blocked(self):
        value = copy.deepcopy(MODELS)
        del value["data"][0]["supportedReasoningEfforts"]
        self.assert_status(m.evaluate_models([value]), "BLOCKED", "MODEL_EFFORT_UNKNOWN")
        for pages in ([], None, [{}], [{"data": None, "nextCursor": None}],
                      [{"data": [None], "nextCursor": None}]):
            with self.subTest(pages=pages):
                self.assert_status(m.evaluate_models(pages), "BLOCKED")

    def test_model_fifth_page_can_finish_but_six_pages_are_not_allowed(self):
        pages = [{"data": [], "nextCursor": "opaque-%d" % index} for index in range(4)]
        pages.append(copy.deepcopy(MODELS))
        self.assert_status(m.evaluate_models(pages), "PASS")
        pages.append(copy.deepcopy(MODELS))
        self.assert_status(m.evaluate_models(pages), "BLOCKED")

    def test_limits_known_windows_are_only_a_quota_pass(self):
        self.assert_status(m.evaluate_limits(copy.deepcopy(LIMITS)), "PASS")

    def test_all_returned_buckets_are_checked(self):
        result = copy.deepcopy(LIMITS)
        depleted = copy.deepcopy(QUOTA)
        depleted["secondary"]["usedPercent"] = 100
        result["rateLimitsByLimitId"] = {"bucket-a": copy.deepcopy(QUOTA), "bucket-b": depleted}
        self.assert_status(m.evaluate_limits(result), "BLOCKED")
        result["rateLimitsByLimitId"]["bucket-b"] = None
        self.assert_status(m.evaluate_limits(result), "BLOCKED")

    def test_depletion_marker_and_percent_block_ready(self):
        for change in ({"rateLimitReachedType": "included-usage"},
                       {"primary": dict(WINDOW, usedPercent=100)},
                       {"secondary": dict(WINDOW, usedPercent=100)}):
            result = copy.deepcopy(LIMITS)
            result["rateLimits"].update(change)
            with self.subTest(change=change):
                self.assert_status(m.evaluate_limits(result), "BLOCKED")

    def test_bad_quota_values_do_not_become_zero_or_reset(self):
        for key, wrong in (("usedPercent", -1), ("usedPercent", 101), ("usedPercent", True),
                           ("usedPercent", float("nan")), ("usedPercent", float("inf")),
                           ("resetsAt", None), ("resetsAt", True), ("resetsAt", 1.5),
                           ("resetsAt", float("nan")), ("resetsAt", float("inf")),
                           ("windowDurationMins", None), ("windowDurationMins", True),
                           ("windowDurationMins", 1.5),
                           ("windowDurationMins", float("nan")),
                           ("windowDurationMins", float("inf"))):
            result = copy.deepcopy(LIMITS)
            result["rateLimits"]["primary"][key] = wrong
            with self.subTest(key=key, wrong=repr(wrong)):
                self.assert_status(m.evaluate_limits(result), "BLOCKED")
        for result in ({}, None, {"rateLimits": None}, {"rateLimits": {}},
                       {"rateLimitsByLimitId": {}}):
            with self.subTest(result=result):
                self.assert_status(m.evaluate_limits(result), "BLOCKED")

    def test_elapsed_reset_does_not_prove_usage_reset(self):
        result = copy.deepcopy(LIMITS)
        result["rateLimits"]["primary"].update(usedPercent=100, resetsAt=1)
        self.assert_status(m.evaluate_limits(result), "BLOCKED")

    def test_credit_enforcement_always_remains_unverified(self):
        for snapshot in (None, {}, copy.deepcopy(LIMITS),
                         {"credits": {"balance": "0", "hasCredits": False}},
                         {"credits": {"balance": "500", "hasCredits": True}},
                         {"rateLimitResetCredits": {"availableCount": 10}},
                         {"local_disable_credits": True, "unknown_secret": SECRET}):
            with self.subTest(snapshot=snapshot):
                self.assertEqual(m.credit_gate(snapshot), {
                    "status": "BLOCKED", "reason": "CREDIT_ENFORCEMENT_UNVERIFIED"
                })

    def test_pure_summaries_discard_private_upstream_values(self):
        for evaluator, value in ((m.evaluate_config, CONFIG), (m.evaluate_account, ACCOUNT),
                                 (m.evaluate_models, [MODELS]), (m.evaluate_limits, LIMITS),
                                 (m.credit_gate, LIMITS)):
            with self.subTest(evaluator=evaluator.__name__):
                self.assert_public(evaluator(copy.deepcopy(value)))


class RpcContractTests(ReadinessAssertions):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "requests.jsonl"
        self.pid_file = self.root / "pid"

    def tearDown(self):
        self.temp.cleanup()

    def command(self, **changes):
        fixture = scenario(log=str(self.log), pid=str(self.pid_file), **changes)
        return [sys.executable, "-u", "-c", FAKE_SERVER, json.dumps(fixture)]

    def client(self, **changes):
        return m.RpcClient(self.command(**changes), cwd=self.root,
                           total_timeout=3, request_timeout=0.3)

    def initialize(self, client):
        client.call("initialize", {"clientInfo": {
            "name": "readiness-test", "title": "synthetic fixture", "version": "2"
        }})
        client.notify_initialized()

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def assert_reaped(self):
        self.assertTrue(self.pid_file.exists())
        pid = int(self.pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_read_snapshot_uses_only_initialized_read_allowlist(self):
        with self.client() as client:
            snapshot = m.read_allowed_snapshot(client)
        requests = self.requests()
        self.assertEqual([row["method"] for row in requests], [
            "initialize", "initialized", "config/read", "account/read", "model/list",
            "account/rateLimits/read",
        ])
        self.assertEqual(requests[1].get("params"), {})
        self.assertNotIn("id", requests[1])
        self.assertEqual(requests[2]["params"], {"includeLayers": False})
        self.assertEqual(requests[3]["params"], {"refreshToken": False})
        self.assertEqual(requests[4]["params"], {"limit": 20, "includeHidden": False})
        # JSONRPC의 무인자 조회는 params 생략과 빈 객체가 같은 의미다.
        self.assertEqual(requests[5].get("params", {}), {})
        self.assertEqual(set(snapshot), {"config", "account", "models", "limits"})
        self.assertEqual(snapshot["account"]["account"]["type"], "chatgpt")
        self.assert_reaped()

    def test_api_auth_stops_before_model_or_quota_lookup(self):
        account = copy.deepcopy(ACCOUNT)
        account["account"]["type"] = "apiKey"
        with self.client(account=account) as client:
            with self.assertRaises(m.ProbeError) as caught:
                m.read_allowed_snapshot(client)
        self.assertEqual(caught.exception.code, "ACCOUNT_AUTH_METHOD_MISMATCH")
        methods = [row["method"] for row in self.requests()]
        self.assertNotIn("model/list", methods)
        self.assertNotIn("account/rateLimits/read", methods)
        self.assert_reaped()

    def test_failed_initialize_cannot_emit_initialized_or_read_queries(self):
        with self.client(initialize_error=True) as client:
            with self.assertRaises(m.ProbeError) as caught:
                m.read_allowed_snapshot(client)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual([row["method"] for row in self.requests()], ["initialize"])
        self.assert_reaped()

    def test_mutating_and_model_methods_are_rejected_before_writing_rpc(self):
        with self.client() as client:
            self.initialize(client)
            for method in ("thread/start", "turn/start", "account/login/start", "account/logout",
                           "config/value/write", "account/rateLimits/consumeResetCredit"):
                with self.subTest(method=method):
                    with self.assertRaises(m.ProbeError):
                        client.call(method, {"secret": SECRET})
        self.assertEqual([row["method"] for row in self.requests()], ["initialize", "initialized"])
        self.assert_reaped()

    def test_pagination_passes_opaque_cursor_without_assuming_last_page(self):
        pages = [{"data": [], "nextCursor": CURSOR}, copy.deepcopy(MODELS)]
        with self.client(pages=pages) as client:
            snapshot = m.read_allowed_snapshot(client)
        self.assert_status(m.evaluate_models(snapshot["models"]), "PASS")
        calls = [row for row in self.requests() if row["method"] == "model/list"]
        self.assertEqual(len(calls), 2)
        self.assertNotIn("cursor", calls[0]["params"])
        self.assertEqual(calls[1]["params"]["cursor"], CURSOR)

    def test_bad_repeat_and_unfinished_cursors_are_blocked(self):
        cases = (
            ([{"data": [], "nextCursor": {}}], "MODEL_CURSOR_INVALID", 1),
            ([{"data": []}], "MODEL_CURSOR_INVALID", 1),
            ([{"data": [], "nextCursor": CURSOR}] * 2, "MODEL_CURSOR_REPEATED", 2),
            ([{"data": [], "nextCursor": "page-%d" % index} for index in range(6)],
             "MODEL_PAGE_LIMIT", 5),
        )
        for pages, reason, expected_calls in cases:
            self.log.unlink(missing_ok=True)
            with self.subTest(reason=reason):
                with self.client(pages=pages) as client:
                    with self.assertRaises(m.ProbeError) as caught:
                        m.read_allowed_snapshot(client)
                self.assertEqual(caught.exception.code, reason)
                calls = [row for row in self.requests() if row["method"] == "model/list"]
                self.assertEqual(len(calls), expected_calls)
                self.assert_reaped()

    def test_protocol_failures_raise_without_raw_error_or_secret(self):
        for mode in ("wrong_id", "server_request", "malformed", "invalid_utf8",
                     "stdout_line", "stdout_total", "stderr_total", "notifications", "rpc_error"):
            with self.subTest(mode=mode):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    with self.client(mode=mode) as client:
                        self.initialize(client)
                        with self.assertRaises(m.ProbeError) as caught:
                            client.call("account/read", {"refreshToken": False})
                for value in (str(caught.exception), out.getvalue(), err.getvalue()):
                    self.assertNotIn(SECRET, value)
                    self.assertNotIn(EMAIL, value)
                self.assert_reaped()

    def test_duplicate_response_cannot_satisfy_another_request(self):
        with self.client(mode="duplicate_id") as client:
            self.initialize(client)
            client.call("account/read", {"refreshToken": False})
            with self.assertRaises(m.ProbeError):
                client.call("config/read", {"includeLayers": False})
        self.assert_reaped()

    def test_timeout_closes_and_reaps_child_even_if_term_is_ignored(self):
        for mode in ("timeout", "ignore_term"):
            with self.subTest(mode=mode):
                started = time.monotonic()
                with self.client(mode=mode) as client:
                    self.initialize(client)
                    with self.assertRaises(m.ProbeError):
                        client.call("account/read", {"refreshToken": False})
                self.assertLess(time.monotonic() - started, 3.5)
                self.assert_reaped()

    def test_total_deadline_is_not_reset_by_successive_queries(self):
        with m.RpcClient(self.command(mode="slow_read", delay=0.08), cwd=self.root,
                         total_timeout=0.2, request_timeout=0.3) as client:
            with self.assertRaises(m.ProbeError) as caught:
                m.read_allowed_snapshot(client)
        self.assertEqual(caught.exception.code, "RPC_TOTAL_TIMEOUT")
        self.assert_reaped()

    # 계획 승인 후 발견한 P2 결함: 기존 N03의 deadline/후손 회수 기준을 보강한다.
    def test_escaped_cursor_request_write_obeys_deadline_when_stdin_is_not_read(self):
        cursor = "\x00" * 4096
        pages = [{"data": [], "nextCursor": cursor}]
        command = self.command(mode="stdin_stall", pages=pages)
        with m.RpcClient(command, cwd=self.root, total_timeout=0.8, request_timeout=0.15) as client:
            fcntl.fcntl(client.process.stdin.fileno(), fcntl.F_SETPIPE_SZ, 4096)

            def stop_fixture():
                try:
                    os.killpg(client.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

            watchdog = threading.Timer(2, stop_fixture)
            watchdog.daemon = True
            watchdog.start()
            started = time.monotonic()
            try:
                with self.assertRaises(m.ProbeError) as caught:
                    m.read_allowed_snapshot(client)
                elapsed = time.monotonic() - started
                self.assertIn(caught.exception.code, ("RPC_REQUEST_TIMEOUT", "RPC_TOTAL_TIMEOUT"))
                self.assertLess(elapsed, 0.7, "stdin write escaped the request deadline")
            finally:
                watchdog.cancel()
        self.assert_reaped()

    def test_close_kills_group_descendants_after_parent_exit_or_term(self):
        for mode in ("group_parent_exited", "group_parent_term"):
            child_pid_file = self.root / (mode + ".child-pid")
            heartbeat = self.root / (mode + ".heartbeat")
            with self.subTest(mode=mode):
                client = m.RpcClient(self.command(mode=mode, child_pid=str(child_pid_file),
                                                 heartbeat=str(heartbeat)),
                                     cwd=self.root, total_timeout=3, request_timeout=0.3)
                try:
                    ready_until = time.monotonic() + 1
                    while (not child_pid_file.exists() or not heartbeat.exists()) and time.monotonic() < ready_until:
                        time.sleep(0.005)
                    self.assertTrue(child_pid_file.exists(), "fixture child was not ready")
                    self.assertTrue(heartbeat.exists(), "fixture child was not executing")
                    child_pid = int(child_pid_file.read_text())
                    os.kill(child_pid, 0)
                    before_tick = int(heartbeat.read_text())
                    time.sleep(0.04)
                    self.assertGreater(int(heartbeat.read_text()), before_tick)
                    if mode == "group_parent_exited":
                        client.process.wait(timeout=1)
                    started = time.monotonic()
                    client.close()
                    self.assertLess(time.monotonic() - started, 2.5)
                    # 이 환경의 /proc은 PID namespace와 다르다. 직접 자식이 아닌 후손의
                    # zombie 여부를 kill(0)로 오판하지 않고 실제 실행 heartbeat 정지를 본다.
                    stopped_tick = int(heartbeat.read_text())
                    time.sleep(0.06)
                    self.assertEqual(int(heartbeat.read_text()), stopped_tick,
                                     "same-group descendant continued executing after close")
                    self.assertIsNotNone(client.process.poll())
                finally:
                    # 결함 코드에서도 테스트가 fixture 후손을 남기지 않는다.
                    try:
                        os.killpg(client.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    client.close()


class ReportContractTests(ReadinessAssertions):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="readiness-private-path-")
        self.root = Path(self.temp.name)
        self.log = self.root / "requests.jsonl"
        self.cli = self.root / "codex-fixture"
        # 테스트 fixture의 실행 환경을 분리하고 실제 인증 저장소는 읽지 않는다.
        self.environment = patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True)
        self.environment.start()
        self.home = patch.object(m.Path, "home", return_value=self.root)
        self.home.start()

    def tearDown(self):
        self.home.stop()
        self.environment.stop()
        self.temp.cleanup()

    def fixture_cli(self, **changes):
        fixture = scenario(log=str(self.log), **changes)
        script = (
            "#!/usr/bin/env python3\nimport sys\n"
            "if sys.argv[1:] != ['--version']:\n"
            "    sys.argv = [sys.argv[0], %r]\n" % json.dumps(fixture)
        ) + FAKE_SERVER
        self.cli.write_text(script)
        self.cli.chmod(0o755)
        return self.cli

    def args(self, codex=None, expected_uid=None):
        return SimpleNamespace(codex=codex, worktree=self.root,
                               expected_uid=os.getuid() if expected_uid is None else expected_uid)

    def assert_inactive(self, report):
        self.assertEqual(report["model_invocations"], 0)
        self.assertIs(report["worker_started"], False)
        self.assertIs(report["github_changed"], False)
        self.assert_status(report["checks"]["credit_enforcement"], "BLOCKED",
                           "CREDIT_ENFORCEMENT_UNVERIFIED")

    def test_known_metadata_still_cannot_enable_worker_or_credit_gate(self):
        codex = self.fixture_cli()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = m.report(self.args(codex=str(codex)))
        self.assertEqual(result["status"], "BLOCKED")
        self.assert_inactive(result)
        self.assert_status(result["checks"]["auth"], "PASS")
        self.assert_status(result["checks"]["configuration"], "PASS")
        self.assert_status(result["checks"]["model_catalog"], "PASS")
        self.assert_status(result["checks"]["quota"], "PASS")
        self.assert_status(result["checks"]["role_overrides"], "BLOCKED",
                           "ROLE_OVERRIDES_UNVERIFIED")
        self.assert_status(result["checks"]["aggregate_concurrency"], "BLOCKED",
                           "AGGREGATE_CAP_UNVERIFIED")
        self.assert_status(result["checks"]["auth_store_observation"], "BLOCKED")
        self.assert_public(result)
        for public_output in (json.dumps(result), out.getvalue(), err.getvalue()):
            self.assertNotIn(str(self.root), public_output)
            self.assertNotIn(SECRET, public_output)
            self.assertNotIn(EMAIL, public_output)
            self.assertNotIn(ACCOUNT_ID, public_output)

    def test_api_auth_environment_blocks_rpc_without_login_or_fallback(self):
        codex = self.fixture_cli()
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"):
            with self.subTest(key=key):
                with patch.dict(os.environ, {key: SECRET}):
                    result = m.report(self.args(codex=str(codex)))
                self.assertEqual(result["status"], "BLOCKED")
                self.assert_inactive(result)
                self.assertIn("API_AUTH_ENV_PRESENT", json.dumps(result))
                self.assertFalse(self.log.exists(), "API environment must not reach app-server RPC")
                self.assert_public(result)

    def test_wrong_execution_account_fails_before_any_rpc(self):
        codex = self.fixture_cli()
        result = m.report(self.args(codex=str(codex), expected_uid=os.getuid() + 1))
        self.assertEqual(result["status"], "FAIL")
        self.assert_status(result["checks"]["execution_account"], "FAIL",
                           "EXECUTION_ACCOUNT_MISMATCH")
        self.assert_inactive(result)
        self.assertFalse(self.log.exists())

    def test_missing_cli_is_a_blocked_prerequisite_and_not_pass(self):
        result = m.report(self.args(codex=str(self.root / "missing-cli-private-path")))
        self.assertEqual(result["status"], "BLOCKED")
        self.assert_status(result["checks"]["cli"], "BLOCKED", "CLI_MISSING")
        self.assert_inactive(result)
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_rpc_error_summary_drops_server_message_and_private_path(self):
        codex = self.fixture_cli(mode="rpc_error")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = m.report(self.args(codex=str(codex)))
        self.assertNotEqual(result["status"], "PASS")
        self.assert_inactive(result)
        for public_output in (json.dumps(result), out.getvalue(), err.getvalue()):
            self.assertNotIn(SECRET, public_output)
            self.assertNotIn(str(self.root), public_output)

    def test_version_output_with_secret_is_rejected_without_raw_output(self):
        self.cli.write_text("#!/usr/bin/env python3\nprint(%r)\n" % ("codex-cli 1.0.0 " + SECRET))
        self.cli.chmod(0o755)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = m.report(self.args(codex=str(self.cli)))
        self.assertNotEqual(result["status"], "PASS")
        self.assert_inactive(result)
        self.assertFalse(self.log.exists())
        for public_output in (json.dumps(result), out.getvalue(), err.getvalue()):
            self.assertNotIn(SECRET, public_output)
            self.assertNotIn(str(self.root), public_output)

    def test_synthetic_auth_file_change_fails_without_publishing_credentials(self):
        auth = self.root / ".codex" / "auth.json"
        auth.parent.mkdir()
        auth.write_text("synthetic-auth-before")
        codex = self.fixture_cli(auth_write=str(auth))
        result = m.report(self.args(codex=str(codex)))
        self.assertEqual(result["status"], "FAIL")
        self.assert_status(result["checks"]["auth_store_observation"], "FAIL", "AUTH_STORE_CHANGED")
        self.assert_inactive(result)
        self.assert_public(result)
        self.assertNotIn(str(self.root), json.dumps(result))

    # 계획 승인 후 발견한 P2 결함: 기존 N09의 version suffix 비밀 폐기 기준을 보강한다.
    def test_valid_semver_suffix_is_not_published_as_cli_version(self):
        self.fixture_cli()
        script = self.cli.read_text()
        self.cli.write_text(script.replace("codex-cli 0.0.0", "codex-cli 1.0.0-" + SECRET))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = m.report(self.args(codex=str(self.cli)))
        self.assertEqual(result["status"], "BLOCKED")
        self.assert_status(result["checks"]["cli"], "PASS")
        self.assertEqual(result["checks"]["cli"]["version"], "1.0.0")
        self.assert_inactive(result)
        for public_output in (json.dumps(result), out.getvalue(), err.getvalue()):
            self.assertNotIn(SECRET, public_output)
            self.assertNotIn(str(self.root), public_output)


if __name__ == "__main__":
    unittest.main()
