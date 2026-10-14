"""Prepare disabled Codex defaults with one native CAS batch; never run a turn."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import time
import types

READINESS_SHA256 = "5b84534a6e0b3bb19fdd6a2bd6e3c38f23ea89edc205b461751b5c0e857ee399"
CLI_VERSION = "0.162.1"
DEFAULTS = {
    "model": "gpt-6.1-sol",
    "model_reasoning_effort": "xhigh",
    "forced_login_method": "chatgpt",
    "agents.enabled": False,
    "agents.max_concurrent_threads_per_session": 2,
    "agents.default_subagent_model": "gpt-6.1-sol",
    "agents.default_subagent_reasoning_effort": "xhigh",
    "features.multi_agent": False,
    "features.multi_agent_v2": False,
}


class PrepareError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def load_readiness():
    path = Path(__file__).with_name("codex_readiness.py")
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024:
        raise PrepareError("READINESS_SOURCE_UNSAFE")
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != READINESS_SHA256:
        raise PrepareError("READINESS_SOURCE_HASH_MISMATCH")
    # Execute the checked bytes, avoiding a second file read or a shared import.
    module = types.ModuleType("private_config_preparation_readiness")
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    module.ALLOWED_METHODS = module.ALLOWED_METHODS | {"config/batchWrite"}
    return module


def guard_paths(args):
    if os.getuid() != args.expected_uid:
        raise PrepareError("EXECUTION_ACCOUNT_MISMATCH")
    home = Path.home()
    if not args.expected_home.is_absolute() or home != args.expected_home or Path(pwd.getpwuid(os.getuid()).pw_dir) != home:
        raise PrepareError("EXECUTION_HOME_MISMATCH")
    if os.environ.get("CODEX_HOME"):
        raise PrepareError("CODEX_HOME_OVERRIDE_PRESENT")
    if any(os.environ.get(key) for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_ACCESS_TOKEN")):
        raise PrepareError("API_OR_EXTERNAL_AUTH_ENV_PRESENT")
    target = home / ".codex/config.toml"
    launcher = home / ".local/bin/codex"
    if args.codex != launcher:
        raise PrepareError("CLI_PATH_MISMATCH")
    for path in (home, target, launcher):
        for node in (path, *path.parents):
            if node.is_symlink():
                raise PrepareError("PREPARATION_SYMLINK_PATH")
    for directory in (home, target.parent, launcher.parent, launcher.parent.parent):
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != args.expected_uid or info.st_mode & 0o022:
            raise PrepareError("PREPARATION_DIRECTORY_UNSAFE")
    for path in (target, launcher):
        if path.exists():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != args.expected_uid or info.st_mode & 0o022 or info.st_size > 1024 * 1024:
                raise PrepareError("PREPARATION_FILE_UNSAFE")
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise PrepareError("CLI_UNAVAILABLE")
    return target


def user_layer(response, target):
    layers = response.get("layers") if isinstance(response, dict) else None
    if not isinstance(layers, list):
        raise PrepareError("USER_LAYER_UNKNOWN")
    found = []
    for layer in layers:
        name = layer.get("name") if isinstance(layer, dict) else None
        if isinstance(name, dict) and name.get("type") == "user" and name.get("file") == str(target) and "profile" in name and name["profile"] is None:
            found.append(layer)
    if len(found) != 1:
        raise PrepareError("USER_LAYER_NOT_UNIQUE")
    layer = found[0]
    # CLI 0.162.1 omits disabledReason when its Rust Option<String> is None.
    if layer.get("disabledReason") is not None or not isinstance(layer.get("config"), dict):
        raise PrepareError("USER_LAYER_UNUSABLE")
    version = layer.get("version")
    if not isinstance(version, str) or not version or len(version) > 4096:
        raise PrepareError("USER_LAYER_VERSION_UNKNOWN")
    return layer


def same_json(left, right):
    # JSON equality retains the distinction between false and numeric zero.
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def get_value(config, key):
    current = config
    for component in key.split("."):
        if not isinstance(current, dict) or component not in current:
            return None
        current = current[component]
    return current


def expected_layer(config, defaults):
    expected = copy.deepcopy(config)
    agents = expected.get("agents")
    if isinstance(agents, dict) and "max_threads" in agents:
        raise PrepareError("LEGACY_CAP_ALIAS_REQUIRES_MIGRATION")
    for key, value in defaults.items():
        current = expected
        parts = key.split(".")
        for component in parts[:-1]:
            if component not in current:
                current[component] = {}
            if not isinstance(current[component], dict):
                raise PrepareError("CONFIG_EDIT_CONTAINER_CONFLICT")
            current = current[component]
        current[parts[-1]] = value
    return expected


def matches(config, defaults):
    return isinstance(config, dict) and all(type(get_value(config, key)) is type(value) and get_value(config, key) == value for key, value in defaults.items())


def verify_origins(response, layer, target, defaults):
    origins = response.get("origins")
    if not isinstance(origins, dict):
        raise PrepareError("CONFIGURATION_ORIGINS_UNKNOWN")
    for key in defaults:
        parts = key.split(".")
        origin = None
        # CLI 0.162.1 records a structured Boolean feature at its enabled leaf.
        prefixes = [key + ".enabled"] if key == "features.multi_agent_v2" else [".".join(parts[:length]) for length in range(len(parts), 0, -1)]
        for prefix in prefixes:
            if prefix in origins:
                origin = origins[prefix]
                break
        if not isinstance(origin, dict) or not isinstance(origin.get("name"), dict):
            raise PrepareError("CONFIGURATION_ORIGINS_UNKNOWN")
        name = origin["name"]
        if name.get("type") != "user" or name.get("file") != str(target) or "profile" not in name or name["profile"] is not None or origin.get("version") != layer["version"]:
            raise PrepareError("EFFECTIVE_CONFIGURATION_OVERRIDE")


def initialize(client):
    client.call("initialize", {"clientInfo": {"name": "codex_config_preparation", "version": "1.0.0"}})
    client.notify_initialized()


def fresh_read(module, args, deadline):
    with module.RpcClient([str(args.codex), "--strict-config", "app-server", "--listen", "stdio://"], cwd=args.expected_home, total_timeout=max(0.01, deadline - time.monotonic())) as client:
        initialize(client)
        return client.call("config/read", {"includeLayers": True})


def prepare(args, result):
    target = guard_paths(args)
    module = load_readiness()
    before_auth = module._auth_stamp()
    deadline = time.monotonic() + 54
    try:
        if module._version(str(args.codex)) != CLI_VERSION:
            raise PrepareError("CLI_VERSION_NOT_REVIEWED")
        defaults = dict(DEFAULTS)
        if args.workspace_write:
            defaults["sandbox_mode"] = "workspace-write"
        with module.RpcClient([str(args.codex), "--strict-config", "app-server", "--listen", "stdio://"], cwd=args.expected_home, total_timeout=max(0.01, deadline - time.monotonic())) as client:
            snapshot = module.read_allowed_snapshot(client)
            if module.evaluate_account(snapshot["account"])["status"] != "PASS":
                raise PrepareError("CHATGPT_AUTH_UNVERIFIED")
            if module.evaluate_models(snapshot["models"])["status"] != "PASS":
                raise PrepareError("REQUESTED_MODEL_OPTION_UNVERIFIED")
            prior = client.call("config/read", {"includeLayers": True})
            layer = user_layer(prior, target)
            expected = expected_layer(layer["config"], defaults)
            changed = [key for key, value in defaults.items() if type(get_value(layer["config"], key)) is not type(value) or get_value(layer["config"], key) != value]
            result["changed_keys"] = changed
            if not changed:
                if not matches(prior.get("config"), defaults):
                    raise PrepareError("EFFECTIVE_CONFIGURATION_OVERRIDE")
                verify_origins(prior, layer, target, defaults)
                result.update(configuration_prepared=True, reason="CONFIGURATION_ALREADY_PREPARED", configuration_check="PASS")
                return
            if not args.apply:
                result["reason"] = "CONFIGURATION_APPLY_PENDING"
                return
            guard_paths(args)
            result["write_attempted"] = True
            try:
                written = client.call("config/batchWrite", {"filePath": str(target), "expectedVersion": layer["version"], "reloadUserConfig": False,
                    "edits": [{"keyPath": key, "value": defaults[key], "mergeStrategy": "upsert"} for key in changed]})
            except module.ProbeError:
                result["reason"] = "CONFIG_WRITE_UNACKNOWLEDGED"
                # Reconnect for observation; never resend a lost write.
                observed = fresh_read(module, args, deadline)
                after = user_layer(observed, target)
                result["unacknowledged_readback_matches"] = same_json(after["config"], expected) and matches(observed.get("config"), defaults)
                return
            if written.get("status") != "ok" or written.get("overriddenMetadata", True) is not None:
                raise PrepareError("CONFIG_WRITE_OVERRIDDEN_OR_UNKNOWN")
            if written.get("filePath") != str(target) or not isinstance(written.get("version"), str) or not written["version"]:
                raise PrepareError("CONFIG_WRITE_RECEIPT_INVALID")
            result["write_acknowledged"] = True
            observed = client.call("config/read", {"includeLayers": True})
            after = user_layer(observed, target)
            guard_paths(args)
            if after["version"] != written["version"]:
                raise PrepareError("CONFIG_CHANGED_AFTER_WRITE")
            if not same_json(after["config"], expected):
                raise PrepareError("CONFIGURATION_OR_UNRELATED_VALUES_CHANGED")
            if not matches(observed.get("config"), defaults):
                raise PrepareError("EFFECTIVE_CONFIGURATION_OVERRIDE")
            verify_origins(observed, after, target, defaults)
            result.update(configuration_prepared=True, unrelated_values_preserved=True,
                          configuration_check="PASS", reason="DISABLED_PREPARATION_DEFAULTS_VERIFIED")
    finally:
        if module._auth_stamp() != before_auth:
            result.update(configuration_prepared=False, configuration_check="BLOCKED")
            raise PrepareError("AUTH_STORE_CHANGED")


def new_result():
    return {"status": "BLOCKED", "configuration_prepared": False, "configuration_check": "BLOCKED",
        "write_attempted": False, "write_acknowledged": False, "changed_keys": [],
        "model_invocations": 0, "worker_started": False, "github_changed": False,
        "native_environment": "unverified", "credit_enforcement": "UNVERIFIED",
        "role_overrides": "UNVERIFIED", "aggregate_concurrency": "UNVERIFIED",
        "native_cas_and_atomicity_acceptance": "UNVERIFIED"}


class SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        raise PrepareError("INVALID_ARGUMENTS")


def main():
    result = new_result()
    try:
        parser = SafeParser(description=__doc__)
        parser.add_argument("--codex", type=Path, required=True)
        parser.add_argument("--expected-uid", type=int, required=True)
        parser.add_argument("--expected-home", type=Path, required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--workspace-write", action="store_true")
        prepare(parser.parse_args(), result)
    except Exception as error:
        code = getattr(error, "code", "PREPARATION_ENVIRONMENT_OR_SCHEMA_ERROR")
        result["reason"] = code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code) else "PREPARATION_ENVIRONMENT_OR_SCHEMA_ERROR"
        result.update(configuration_prepared=False, configuration_check="BLOCKED")
    except KeyboardInterrupt:
        result["reason"] = "PREPARATION_INTERRUPTED"
        result.update(configuration_prepared=False, configuration_check="BLOCKED")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
