"""D29 config-apply-plan-3의 작은 fixture 검사. Native·모델 실행 인수 아님."""

import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parents[1] / "codex_prepare_config.py"
SPEC = importlib.util.spec_from_file_location("codex_prepare_config_under_test", SOURCE)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)

PIN = "5b84534a6e0b3bb19fdd6a2bd6e3c38f23ea89edc205b461751b5c0e857ee399"
SECRET = "CANARY-NOT-A-REAL-TOKEN-config-preparation"
EMAIL = "canary-config@example.invalid"
ACCOUNT_ID = "canary-account-id-do-not-publish"
POLICY = {
    "model": "gpt-6.1-sol", "model_reasoning_effort": "xhigh",
    "forced_login_method": "chatgpt", "agents.enabled": False,
    "agents.max_concurrent_threads_per_session": 2,
    "agents.default_subagent_model": "gpt-6.1-sol",
    "agents.default_subagent_reasoning_effort": "xhigh",
    "features.multi_agent": False, "features.multi_agent_v2": False,
}


def set_key(config, key, value):
    node = config
    parts = key.split(".")
    for component in parts[:-1]:
        node = node.setdefault(component, {})
    node[parts[-1]] = copy.deepcopy(value)


def prior_config():
    return {
        "model": "old-model", "model_reasoning_effort": "high",
        "forced_login_method": "chatgpt", "sandbox_mode": "read-only",
        "agents": {"enabled": True, "max_concurrent_threads_per_session": 4,
                   "default_subagent_model": "old-model", "default_subagent_reasoning_effort": "high",
                   "reviewer": {"config_file": "keep-this-reference"}},
        "features": {"multi_agent": True, "multi_agent_v2": True, "unrelated_feature": True},
        "profiles": {"existing": {"model": "keep-profile-model", "marker": SECRET}},
        "unrelated": {"bool": False, "numeric_zero": 0, "float": 1.0,
                      "array": ["retain", 3], "private": SECRET},
    }


class FakeNative:
    """버전 결합과 atomic upsert를 모의한다. 공식 서비스 구현의 실제 증거는 아니다."""
    def __init__(self, target, config=None, **options):
        self.target = target
        self.config = copy.deepcopy(config if config is not None else prior_config())
        self.version = "version-before"
        self.options = options
        self.calls = []
        self.connections = 0
        self.closed = 0
        self.module = m.load_readiness()
        self.module._version = lambda _command: options.get("cli_version", "0.162.1")
        stamps = iter(options.get("auth_stamps", [b"fixture-auth", b"fixture-auth"]))
        self.module._auth_stamp = lambda: next(stamps)
        native = self

        class Client:
            def __init__(self, command, cwd=None, total_timeout=60, request_timeout=5):
                native.connections += 1
                self.number = native.connections
                native.calls.append((self.number, "transport", {
                    "command": list(command), "cwd": cwd, "total_timeout": total_timeout,
                    "request_timeout": request_timeout,
                }))

            def __enter__(self):
                return self

            def __exit__(self, *_):
                native.closed += 1

            def notify_initialized(self):
                native.calls.append((self.number, "initialized", {}))

            def call(self, method, params=None):
                native.calls.append((self.number, method, copy.deepcopy(params)))
                if method not in native.module.ALLOWED_METHODS:
                    raise native.module.ProbeError("RPC_METHOD_FORBIDDEN")
                if method == "initialize":
                    return {"userAgent": "fixture"}
                if method == "account/read":
                    return {"account": {"type": native.options.get("account_type", "chatgpt"),
                                        "email": EMAIL, "id": ACCOUNT_ID},
                            "requiresOpenaiAuth": True}
                if method == "model/list":
                    return {"data": [{"model": "gpt-6.1-sol", "id": "fixture-model-id",
                                      "supportedReasoningEfforts": [{"reasoningEffort":
                                          native.options.get("effort", "xhigh")}]}], "nextCursor": None}
                if method == "account/rateLimits/read":
                    return {"rateLimits": {"primary": {"usedPercent": 10,
                        "windowDurationMins": 300, "resetsAt": int(time.time()) + 3600},
                        "credits": {"balance": SECRET}}}
                if method == "config/read":
                    if self.number > 1 and native.options.get("reconnect_failure"):
                        raise native.module.ProbeError("FIXTURE_READBACK_UNKNOWN")
                    return native.read(bool(params and params.get("includeLayers")))
                if method == "config/batchWrite":
                    return native.write(params)
                raise AssertionError("unexpected fixture method")

        self.module.RpcClient = Client

    def read(self, layers):
        effective = copy.deepcopy(self.config)
        if self.options.get("effective_override"):
            effective["model"] = "override-model"
        metadata = {"name": {"type": "user", "file": str(self.target), "profile": None},
                    "version": self.version}
        origins = {key: copy.deepcopy(metadata) for key in POLICY}
        if "sandbox_mode" in self.config:
            origins["sandbox_mode"] = copy.deepcopy(metadata)
        origins["private"] = SECRET
        result = {"config": effective, "origins": origins}
        if layers:
            layer = {"name": {"type": "user", "file": str(self.target), "profile": None},
                     "version": self.version, "config": copy.deepcopy(self.config)}
            # Rust 0.162.1의 Option::None은 skip_serializing_if로 wire에서 생략된다.
            result["layers"] = [layer]
            mutate = self.options.get("layer_mutator")
            if mutate:
                mutate(result)
            mutate = self.options.get("origin_mutator")
            if mutate:
                mutate(result)
        return result

    def write(self, params):
        if params["filePath"] != str(self.target) or params["expectedVersion"] != self.version:
            raise self.module.ProbeError("FIXTURE_CAS_CONFLICT")
        failure = self.options.get("write_failure")
        if failure == "cas_conflict":
            self.config["unrelated"]["concurrent_writer"] = "retained-concurrent-value"
            self.version = "concurrent-version"
        if failure in ("cas_conflict", "unsupported", "rejected_edit", "lost_before_commit"):
            raise self.module.ProbeError("FIXTURE_WRITE_REJECTED")
        updated = copy.deepcopy(self.config)
        for edit in params["edits"]:
            if edit["mergeStrategy"] != "upsert":
                raise AssertionError("fixture only supports atomic scalar upsert")
            set_key(updated, edit["keyPath"], edit["value"])
        self.config = updated
        self.version = "version-after"
        mutate = self.options.get("after_mutator")
        if mutate:
            mutate(self.config)
        if failure == "lost_after_commit":
            raise self.module.ProbeError("FIXTURE_RESPONSE_LOST")
        receipt = {"status": "ok", "version": self.version, "filePath": str(self.target),
                   "overriddenMetadata": None}
        receipt.update(self.options.get("receipt", {}))
        return receipt

    def writes(self):
        return [row for row in self.calls if row[1] == "config/batchWrite"]


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="config-preparation-private-")
        self.home = Path(self.temp.name)
        self.target = self.home / ".codex/config.toml"
        self.launcher = self.home / ".local/bin/codex"
        self.target.parent.mkdir(mode=0o700)
        self.launcher.parent.mkdir(mode=0o700, parents=True)
        self.target.write_text('# retained TOML comment\nunrelated = "kept"\n')
        self.target.chmod(0o600)
        self.launcher.write_text("synthetic launcher; must never execute")
        self.launcher.chmod(0o700)
        self.uid = os.getuid()
        self.patches = [patch.dict(os.environ, {}, clear=True),
                        patch.object(m.Path, "home", return_value=self.home),
                        patch.object(m.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(self.home)))]
        for context in self.patches:
            context.start()

    def tearDown(self):
        for context in reversed(self.patches):
            context.stop()
        self.temp.cleanup()

    def args(self, **changes):
        values = {"expected_uid": self.uid, "expected_home": self.home, "codex": self.launcher,
                  "workspace_write": False, "apply": False}
        values.update(changes)
        return SimpleNamespace(**values)

    def invoke(self, native, **changes):
        result = m.new_result()
        with patch.object(m, "load_readiness", return_value=native.module):
            m.prepare(self.args(**changes), result)
        return result

    def assert_inactive_and_public(self, result):
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["model_invocations"], 0)
        self.assertIs(result["worker_started"], False)
        self.assertIs(result["github_changed"], False)
        self.assertEqual(result["credit_enforcement"], "UNVERIFIED")
        for private in (SECRET, EMAIL, ACCOUNT_ID, str(self.home)):
            self.assertNotIn(private, json.dumps(result))

    def test_c01_single_scalar_cas_preserves_unrelated_configuration(self):
        native = FakeNative(self.target)
        before = copy.deepcopy(native.config)
        untouched_file = self.target.read_bytes()
        result = self.invoke(native, apply=True)
        expected = copy.deepcopy(before)
        for key, value in POLICY.items():
            set_key(expected, key, value)
        self.assertEqual(native.config, expected)
        self.assertEqual(native.config["unrelated"], before["unrelated"])
        self.assertIs(native.config["agents"]["enabled"], False)
        self.assertIs(native.config["features"]["multi_agent"], False)
        self.assertIs(native.config["features"]["multi_agent_v2"], False)
        self.assertEqual(self.target.read_bytes(), untouched_file, "helper must not rewrite TOML directly")
        self.assertTrue(result["configuration_prepared"])
        self.assertTrue(result["unrelated_values_preserved"])
        self.assertEqual(result["configuration_check"], "PASS")
        writes = native.writes()
        self.assertEqual(len(writes), 1)
        payload = writes[0][2]
        self.assertEqual(payload["filePath"], str(self.target))
        self.assertEqual(payload["expectedVersion"], "version-before")
        self.assertIs(payload["reloadUserConfig"], False)
        self.assertEqual({edit["keyPath"]: edit["value"] for edit in payload["edits"]},
                         {key: value for key, value in POLICY.items() if key != "forced_login_method"})
        self.assertTrue(all(edit["mergeStrategy"] == "upsert" for edit in payload["edits"]))
        self.assert_inactive_and_public(result)

    def test_c01_workspace_edit_is_explicitly_opt_in(self):
        native = FakeNative(self.target)
        result = self.invoke(native, apply=True, workspace_write=True)
        self.assertEqual(native.config["sandbox_mode"], "workspace-write")
        edits = native.writes()[0][2]["edits"]
        self.assertIn({"keyPath": "sandbox_mode", "value": "workspace-write", "mergeStrategy": "upsert"}, edits)
        self.assert_inactive_and_public(result)

    def test_c02_dry_run_and_prepared_noop_never_write(self):
        native = FakeNative(self.target)
        result = self.invoke(native)
        self.assertFalse(result["configuration_prepared"])
        self.assertEqual(result["reason"], "CONFIGURATION_APPLY_PENDING")
        self.assertEqual(native.writes(), [])
        self.assert_inactive_and_public(result)
        ready = prior_config()
        for key, value in POLICY.items():
            set_key(ready, key, value)
        native = FakeNative(self.target, config=ready)
        result = self.invoke(native, apply=True)
        self.assertTrue(result["configuration_prepared"])
        self.assertEqual(native.writes(), [])
        self.assert_inactive_and_public(result)

    def test_c02_identity_home_launcher_and_auth_environment_guards(self):
        cases = ((self.args(expected_uid=self.uid + 1), "EXECUTION_ACCOUNT_MISMATCH"),
                 (self.args(expected_home=self.home / "other"), "EXECUTION_HOME_MISMATCH"),
                 (self.args(codex=self.home / "other-codex"), "CLI_PATH_MISMATCH"))
        for args, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(m.PrepareError) as caught:
                m.guard_paths(args)
            self.assertEqual(caught.exception.code, reason)
        for name in ("CODEX_HOME", "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_ACCESS_TOKEN"):
            with self.subTest(name=name), patch.dict(os.environ, {name: SECRET}):
                with self.assertRaises(m.PrepareError):
                    m.guard_paths(self.args())
        with patch.object(m.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(self.home / "other"))):
            with self.assertRaises(m.PrepareError):
                m.guard_paths(self.args())

    def test_c02_symlink_and_writable_paths_are_rejected(self):
        self.target.unlink()
        self.target.symlink_to(self.home / "other-config")
        with self.assertRaises(m.PrepareError) as caught:
            m.guard_paths(self.args())
        self.assertEqual(caught.exception.code, "PREPARATION_SYMLINK_PATH")
        self.target.unlink()
        self.target.write_text("fixture")
        self.target.chmod(0o660)
        with self.assertRaises(m.PrepareError) as caught:
            m.guard_paths(self.args())
        self.assertEqual(caught.exception.code, "PREPARATION_FILE_UNSAFE")
        self.target.chmod(0o600)
        self.target.parent.chmod(0o770)
        with self.assertRaises(m.PrepareError) as caught:
            m.guard_paths(self.args())
        self.assertEqual(caught.exception.code, "PREPARATION_DIRECTORY_UNSAFE")

    def test_c02_foreign_owner_is_rejected_without_chown(self):
        original = m.Path.stat

        def foreign_stat(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == self.target:
                return SimpleNamespace(st_mode=info.st_mode, st_uid=self.uid + 1, st_size=info.st_size)
            return info

        with patch.object(m.Path, "stat", new=foreign_stat):
            with self.assertRaises(m.PrepareError) as caught:
                m.guard_paths(self.args())
        self.assertEqual(caught.exception.code, "PREPARATION_FILE_UNSAFE")

    def test_c02_symlink_config_ancestor_is_rejected_before_rpc(self):
        actual = self.home / "actual-config-directory"
        self.target.parent.rename(actual)
        self.target.parent.symlink_to(actual, target_is_directory=True)
        with patch.object(m, "load_readiness", side_effect=AssertionError("guard must run before RPC loading")):
            with self.assertRaises(m.PrepareError) as caught:
                m.prepare(self.args(apply=True), m.new_result())
        self.assertEqual(caught.exception.code, "PREPARATION_SYMLINK_PATH")

    def test_c02_unusable_layers_do_not_reach_batch_write(self):
        def missing(response):
            response["layers"] = []

        def duplicate(response):
            response["layers"].append(copy.deepcopy(response["layers"][0]))

        mutations = [missing, duplicate,
            lambda r: r["layers"][0].update(disabledReason="managed"),
            lambda r: r["layers"][0].update(version=""),
            lambda r: r["layers"][0].update(version=None),
            lambda r: r["layers"][0]["name"].pop("profile"),
            lambda r: r["layers"][0]["name"].update(profile="other-profile"),
            lambda r: r["layers"][0]["name"].update(file=str(self.home / "other-config"))]
        for index, mutation in enumerate(mutations):
            native = FakeNative(self.target, layer_mutator=mutation)
            with self.subTest(index=index), self.assertRaises(m.PrepareError):
                self.invoke(native, apply=True)
            self.assertEqual(native.writes(), [])

    def test_w01_w02_enabled_omission_and_explicit_null_have_equal_results(self):
        # 승인된 wire 보정: 필드 생략은 실제 비활성 reason이 아니라 활성 None 표현이다.
        normal = FakeNative(self.target)
        self.assertNotIn("disabledReason", normal.read(True)["layers"][0])
        omitted_result = self.invoke(normal, apply=True)
        compatible = FakeNative(self.target, layer_mutator=
            lambda response: response["layers"][0].update(disabledReason=None))
        self.assertIsNone(compatible.read(True)["layers"][0]["disabledReason"])
        explicit_null_result = self.invoke(compatible, apply=True)
        self.assertEqual(omitted_result, explicit_null_result)
        self.assertTrue(omitted_result["configuration_prepared"])
        self.assertEqual(omitted_result["configuration_check"], "PASS")
        self.assertEqual(len(normal.writes()), 1)
        self.assertEqual(len(compatible.writes()), 1)
        self.assert_inactive_and_public(omitted_result)
        self.assert_inactive_and_public(explicit_null_result)

    def test_w03_every_present_nonnull_disabled_reason_blocks_write(self):
        for reason in ("managed", False, 0, "", [], {}):
            native = FakeNative(self.target, layer_mutator=
                lambda response, reason=reason: response["layers"][0].update(disabledReason=reason))
            with self.subTest(reason=reason), self.assertRaises(m.PrepareError) as caught:
                self.invoke(native, apply=True)
            self.assertEqual(caught.exception.code, "USER_LAYER_UNUSABLE")
            self.assertEqual(native.writes(), [])

    def test_w04_missing_or_malformed_layer_config_still_blocks_write(self):
        mutations = [lambda response: response["layers"][0].pop("config")]
        for invalid in (None, [], "invalid-layer-config"):
            mutations.append(lambda response, invalid=invalid:
                             response["layers"][0].update(config=invalid))
        for index, mutation in enumerate(mutations):
            native = FakeNative(self.target, layer_mutator=mutation)
            with self.subTest(index=index), self.assertRaises(m.PrepareError) as caught:
                self.invoke(native, apply=True)
            self.assertEqual(caught.exception.code, "USER_LAYER_UNUSABLE")
            self.assertEqual(native.writes(), [])

    def test_c02_account_model_and_legacy_cap_block_before_write(self):
        cases = (({"account_type": "apiKey"}, "ACCOUNT_AUTH_METHOD_MISMATCH"),
                 ({"effort": "high"}, "REQUESTED_MODEL_OPTION_UNVERIFIED"),
                 ({"cli_version": "0.162.0"}, "CLI_VERSION_NOT_REVIEWED"))
        for options, reason in cases:
            native = FakeNative(self.target, **options)
            expected_error = native.module.ProbeError if "account_type" in options else m.PrepareError
            with self.subTest(options=options), self.assertRaises(expected_error) as caught:
                self.invoke(native, apply=True)
            self.assertEqual(caught.exception.code, reason)
            self.assertEqual(native.writes(), [])
        config = prior_config()
        config["agents"]["max_threads"] = 2
        native = FakeNative(self.target, config=config)
        with self.assertRaises(m.PrepareError) as caught:
            self.invoke(native, apply=True)
        self.assertEqual(caught.exception.code, "LEGACY_CAP_ALIAS_REQUIRES_MIGRATION")
        self.assertEqual(native.writes(), [])

    def test_c03_cas_conflict_or_atomic_rejection_never_resends(self):
        for failure in ("cas_conflict", "rejected_edit", "unsupported", "lost_before_commit"):
            native = FakeNative(self.target, write_failure=failure)
            before = copy.deepcopy(native.config)
            with self.subTest(failure=failure):
                result = self.invoke(native, apply=True)
                self.assertFalse(result["configuration_prepared"])
                self.assertFalse(result["write_acknowledged"])
                self.assertEqual(len(native.writes()), 1)
                self.assertEqual(native.connections, 2, "uncertain write must be observed on a fresh connection")
                self.assertEqual(native.closed, 2)
                self.assertEqual(native.config["model"], before["model"])
                if failure == "cas_conflict":
                    self.assertEqual(native.config["unrelated"]["concurrent_writer"], "retained-concurrent-value")
                else:
                    self.assertEqual(native.config, before)
                self.assert_inactive_and_public(result)

    def test_c04_lost_success_is_observed_but_not_rewritten_or_prepared(self):
        native = FakeNative(self.target, write_failure="lost_after_commit")
        result = self.invoke(native, apply=True)
        self.assertTrue(result["unacknowledged_readback_matches"])
        self.assertFalse(result["configuration_prepared"])
        self.assertEqual(result["configuration_check"], "BLOCKED")
        self.assertEqual(len(native.writes()), 1)
        self.assertEqual(native.connections, 2)
        self.assert_inactive_and_public(result)

    def test_c04_unavailable_reconnect_remains_unknown_without_resending(self):
        native = FakeNative(self.target, write_failure="lost_after_commit", reconnect_failure=True)
        result = m.new_result()
        with patch.object(m, "load_readiness", return_value=native.module):
            with self.assertRaises(native.module.ProbeError) as caught:
                m.prepare(self.args(apply=True), result)
        self.assertEqual(caught.exception.code, "FIXTURE_READBACK_UNKNOWN")
        self.assertFalse(result["configuration_prepared"])
        self.assertEqual(len(native.writes()), 1)
        self.assertEqual(native.connections, 2)
        self.assert_inactive_and_public(result)

    def test_c04_receipt_override_unknown_or_other_target_cannot_prepare(self):
        for receipt in ({"status": "okOverridden"}, {"overriddenMetadata": {"private": SECRET}},
                        {"status": "unknown"}, {"filePath": str(self.home / "other-config")},
                        {"version": ""}, {"version": None}, {"version": "wrong-version"}):
            native = FakeNative(self.target, receipt=receipt)
            with self.subTest(receipt=receipt), self.assertRaises(m.PrepareError):
                self.invoke(native, apply=True)
            self.assertEqual(len(native.writes()), 1)

    def test_c04_typed_false_unrelated_values_and_effective_override_are_verified(self):
        mutations = (lambda c: c["features"].update(multi_agent_v2=0),
                     lambda c: c["agents"].update(enabled="false"),
                     lambda c: c["unrelated"].update(bool=0),
                     lambda c: c["unrelated"].update(private="changed"))
        for index, mutation in enumerate(mutations):
            native = FakeNative(self.target, after_mutator=mutation)
            with self.subTest(index=index), self.assertRaises(m.PrepareError):
                self.invoke(native, apply=True)
            self.assertEqual(len(native.writes()), 1)
        native = FakeNative(self.target, effective_override=True)
        with self.assertRaises(m.PrepareError) as caught:
            self.invoke(native, apply=True)
        self.assertEqual(caught.exception.code, "EFFECTIVE_CONFIGURATION_OVERRIDE")

    # 사전 계획의 override 차단 기준을 같은 값/다른 출처까지 보강한다.
    def test_c04_same_value_higher_layer_blocks_noop_and_acknowledged_write(self):
        def higher_layer(response):
            response["origins"]["model"] = {
                "name": {"type": "project", "file": str(self.home / "project/.codex/config.toml"),
                         "profile": None}, "version": "higher-layer-version"}

        for already_prepared in (False, True):
            config = prior_config()
            if already_prepared:
                for key, value in POLICY.items():
                    set_key(config, key, value)
            native = FakeNative(self.target, config=config, origin_mutator=higher_layer)
            result = m.new_result()
            with self.subTest(already_prepared=already_prepared), patch.object(m, "load_readiness", return_value=native.module):
                with self.assertRaises(m.PrepareError) as caught:
                    m.prepare(self.args(apply=True), result)
            self.assertEqual(caught.exception.code, "EFFECTIVE_CONFIGURATION_OVERRIDE")
            self.assertFalse(result["configuration_prepared"])
            self.assertEqual(len(native.writes()), 0 if already_prepared else 1)
            self.assert_inactive_and_public(result)

    def test_c04_missing_origins_block_noop_and_acknowledged_write(self):
        for already_prepared in (False, True):
            for mutation in (lambda r: r.pop("origins"), lambda r: r.update(origins={}),
                             lambda r: r.update(origins=[])):
                config = prior_config()
                if already_prepared:
                    for key, value in POLICY.items():
                        set_key(config, key, value)
                native = FakeNative(self.target, config=config, origin_mutator=mutation)
                result = m.new_result()
                with self.subTest(already_prepared=already_prepared, mutation=mutation), patch.object(m, "load_readiness", return_value=native.module):
                    with self.assertRaises(m.PrepareError) as caught:
                        m.prepare(self.args(apply=True), result)
                self.assertEqual(caught.exception.code, "CONFIGURATION_ORIGINS_UNKNOWN")
                self.assertFalse(result["configuration_prepared"])
                self.assertEqual(len(native.writes()), 0 if already_prepared else 1)
                self.assert_inactive_and_public(result)

    def test_c04_parent_prefix_is_valid_but_nearest_origin_takes_precedence(self):
        def grouped_origins(response):
            origins = response["origins"]
            origins["agents"] = copy.deepcopy(origins["agents.enabled"])
            origins["features"] = copy.deepcopy(origins["features.multi_agent"])
            for key in list(origins):
                if key.startswith("agents.") or key.startswith("features."):
                    del origins[key]

        native = FakeNative(self.target, origin_mutator=grouped_origins)
        result = self.invoke(native, apply=True)
        self.assertTrue(result["configuration_prepared"])
        self.assert_inactive_and_public(result)

        def nearest_override(response):
            grouped_origins(response)
            metadata = copy.deepcopy(response["origins"]["agents"])
            metadata["name"]["profile"] = "higher-profile"
            response["origins"]["agents.enabled"] = metadata

        native = FakeNative(self.target, origin_mutator=nearest_override)
        with self.assertRaises(m.PrepareError) as caught:
            self.invoke(native, apply=True)
        self.assertEqual(caught.exception.code, "EFFECTIVE_CONFIGURATION_OVERRIDE")

    def test_c05_private_transport_extends_only_batch_and_keeps_source_pinned(self):
        path = SOURCE.with_name("codex_readiness.py")
        before = path.read_bytes()
        self.assertEqual(hashlib.sha256(before).hexdigest(), PIN)
        first, second = m.load_readiness(), m.load_readiness()
        self.assertIsNot(first, second)
        self.assertEqual(first.ALLOWED_METHODS, {
            "initialize", "config/read", "account/read", "model/list",
            "account/rateLimits/read", "config/batchWrite"})
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), PIN)
        spec = importlib.util.spec_from_file_location("original_readonly_fixture", path)
        original = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(original)
        self.assertNotIn("config/batchWrite", original.ALLOWED_METHODS)
        self.assertNotIn("turn/start", first.ALLOWED_METHODS)

    def test_c05_hash_mismatch_blocks_before_unreviewed_code_exec(self):
        with patch.object(m.Path, "read_bytes", return_value=b"raise RuntimeError('UNREVIEWED_EXECUTED')"):
            with self.assertRaises(m.PrepareError) as caught:
                m.load_readiness()
        self.assertEqual(caught.exception.code, "READINESS_SOURCE_HASH_MISMATCH")

    def test_c06_only_read_metadata_and_one_pinned_batch_are_called(self):
        native = FakeNative(self.target)
        result = self.invoke(native, apply=True)
        methods = [row[1] for row in native.calls if row[1] != "transport"]
        self.assertEqual(methods, ["initialize", "initialized", "config/read", "account/read",
            "model/list", "account/rateLimits/read", "config/read", "config/batchWrite", "config/read"])
        account = next(row for row in native.calls if row[1] == "account/read")
        self.assertEqual(account[2], {"refreshToken": False})
        for _, method, params in native.calls:
            if method == "transport":
                self.assertGreater(params["total_timeout"], 0)
                self.assertLessEqual(params["total_timeout"], 60)
        self.assert_inactive_and_public(result)

    def test_c06_auth_change_revokes_configuration_prepared(self):
        native = FakeNative(self.target, auth_stamps=[b"before", b"after"])
        result = m.new_result()
        with patch.object(m, "load_readiness", return_value=native.module):
            with self.assertRaises(m.PrepareError) as caught:
                m.prepare(self.args(apply=True), result)
        self.assertEqual(caught.exception.code, "AUTH_STORE_CHANGED")
        self.assertFalse(result["configuration_prepared"])
        self.assertEqual(result["configuration_check"], "BLOCKED")
        self.assert_inactive_and_public(result)

    def test_c06_cli_renderer_drops_raw_errors_and_account_configuration(self):
        args = ["codex_prepare_config", "--codex", str(self.launcher),
                "--expected-uid", str(self.uid), "--expected-home", str(self.home), "--apply"]
        native = FakeNative(self.target)
        for failure in (None, RuntimeError(SECRET)):
            out, err = io.StringIO(), io.StringIO()
            with self.subTest(failure=type(failure).__name__), patch.object(sys, "argv", args):
                with redirect_stdout(out), redirect_stderr(err), patch.object(m, "load_readiness", return_value=native.module):
                    if failure is None:
                        m.main()
                    else:
                        with patch.object(m, "prepare", side_effect=failure):
                            m.main()
                result = json.loads(out.getvalue())
                self.assert_inactive_and_public(result)
                for private in (SECRET, EMAIL, ACCOUNT_ID, str(self.home)):
                    self.assertNotIn(private, out.getvalue() + err.getvalue())

    def test_c06_interrupt_after_pass_revokes_prepared_in_public_result(self):
        def interrupted_after_pass(_args, result):
            result.update(configuration_prepared=True, configuration_check="PASS")
            raise KeyboardInterrupt()

        args = ["codex_prepare_config", "--codex", str(self.launcher),
                "--expected-uid", str(self.uid), "--expected-home", str(self.home), "--apply"]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", args), patch.object(m, "prepare", side_effect=interrupted_after_pass):
            with redirect_stdout(out), redirect_stderr(err):
                m.main()
        result = json.loads(out.getvalue())
        self.assertFalse(result["configuration_prepared"])
        self.assertEqual(result["configuration_check"], "BLOCKED")
        self.assertEqual(result["reason"], "PREPARATION_INTERRUPTED")
        self.assert_inactive_and_public(result)


if __name__ == "__main__":
    unittest.main()
