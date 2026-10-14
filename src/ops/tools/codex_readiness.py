"""Bounded, read-only Codex metadata probe; never starts a model turn or worker."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import time

ALLOWED_METHODS = frozenset({"initialize", "config/read", "account/read",
                           "model/list", "account/rateLimits/read"})
MAX_LINE = 128 * 1024
MAX_STDOUT = 512 * 1024
MAX_STDERR = 64 * 1024
MAX_NOTIFICATIONS = 32
MAX_MODEL_PAGES = 5


class ProbeError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def item(status, reason):
    return {"status": status, "reason": reason}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProbeError("RPC_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _parse(data):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ProbeError("RPC_NONFINITE_JSON")))
    except (ValueError, UnicodeError, RecursionError):
        raise ProbeError("RPC_INVALID_JSON") from None


class RpcClient:
    def __init__(self, command, cwd=None, total_timeout=60, request_timeout=5):
        if not 0 < total_timeout <= 60 or not 0 < request_timeout <= 5:
            raise ProbeError("TIMEOUT_ARGUMENT_INVALID")
        self.deadline = time.monotonic() + total_timeout
        self.request_timeout = request_timeout
        self.process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        bufsize=0, start_new_session=True)
        self.selector = selectors.DefaultSelector()
        os.set_blocking(self.process.stdin.fileno(), False)
        for stream in (self.process.stdout, self.process.stderr):
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, selectors.EVENT_READ)
        self.buffer = bytearray()
        self.stdout_bytes = self.stderr_bytes = self.notifications = 0
        self.next_id = 1
        self.completed_ids = set()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        # Descendants may keep the group alive even after its leader exits.
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        finally:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.process.wait(timeout=1)
        self.selector.close()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()

    def _send(self, value, deadline=None):
        data = (json.dumps(value, separators=(",", ":")) + "\n").encode()
        if len(data) > MAX_LINE:
            raise ProbeError("RPC_REQUEST_TOO_LARGE")
        deadline = min(self.deadline, deadline or time.monotonic() + self.request_timeout)
        offset = 0
        with selectors.DefaultSelector() as writer:
            writer.register(self.process.stdin, selectors.EVENT_WRITE)
            for key in self.selector.get_map().values():
                writer.register(key.fileobj, selectors.EVENT_READ)
            while offset < len(data):
                now = time.monotonic()
                if now >= self.deadline:
                    raise ProbeError("RPC_TOTAL_TIMEOUT")
                if now >= deadline:
                    raise ProbeError("RPC_REQUEST_TIMEOUT")
                for key, _ in writer.select(deadline - now):
                    if key.fileobj is not self.process.stdin:
                        if not self._read_stream(key.fileobj):
                            writer.unregister(key.fileobj)
                        continue
                    try:
                        offset += os.write(self.process.stdin.fileno(), data[offset:])
                    except BlockingIOError:
                        continue
                    except (BrokenPipeError, OSError):
                        raise ProbeError("RPC_PROCESS_CLOSED") from None

    def notify_initialized(self):
        self._send({"method": "initialized", "params": {}})

    def _line(self, deadline):
        while True:
            now = time.monotonic()
            if now >= self.deadline:
                raise ProbeError("RPC_TOTAL_TIMEOUT")
            if now >= deadline:
                raise ProbeError("RPC_REQUEST_TIMEOUT")
            split = self.buffer.find(b"\n")
            if split >= 0:
                if split > MAX_LINE:
                    raise ProbeError("RPC_LINE_LIMIT")
                line = bytes(self.buffer[:split])
                del self.buffer[:split + 1]
                return line
            if len(self.buffer) > MAX_LINE:
                raise ProbeError("RPC_LINE_LIMIT")
            events = self.selector.select(min(deadline, self.deadline) - now)
            for key, _ in events:
                self._read_stream(key.fileobj)
            if not self.selector.get_map() and not self.buffer:
                raise ProbeError("RPC_PROCESS_CLOSED")

    def _read_stream(self, stream):
        try:
            chunk = os.read(stream.fileno(), 8192)
        except BlockingIOError:
            return True
        if not chunk:
            self.selector.unregister(stream)
            return False
        if stream is self.process.stdout:
            self.stdout_bytes += len(chunk)
            if self.stdout_bytes > MAX_STDOUT:
                raise ProbeError("RPC_STDOUT_LIMIT")
            self.buffer.extend(chunk)
        else:
            self.stderr_bytes += len(chunk)
            if self.stderr_bytes > MAX_STDERR:
                raise ProbeError("RPC_STDERR_LIMIT")
            # Error messages may contain credentials: discard them immediately.
        return True

    def call(self, method, params=None):
        if method not in ALLOWED_METHODS:
            raise ProbeError("RPC_METHOD_FORBIDDEN")
        request_id = self.next_id
        self.next_id += 1
        request = {"method": method, "id": request_id}
        if params is not None:
            request["params"] = params
        deadline = time.monotonic() + self.request_timeout
        self._send(request, deadline)
        while True:
            response = _parse(self._line(deadline))
            if not isinstance(response, dict):
                raise ProbeError("RPC_RESPONSE_INVALID")
            if "method" in response:
                if "id" in response:
                    raise ProbeError("RPC_SERVER_REQUEST_FORBIDDEN")
                self.notifications += 1
                if self.notifications > MAX_NOTIFICATIONS:
                    raise ProbeError("RPC_NOTIFICATION_LIMIT")
                continue
            response_id = response.get("id")
            if type(response_id) is int and response_id in self.completed_ids:
                raise ProbeError("RPC_DUPLICATE_RESPONSE")
            if type(response_id) is not int or response_id != request_id:
                raise ProbeError("RPC_RESPONSE_ID_MISMATCH")
            if "error" in response:
                raise ProbeError("RPC_REMOTE_ERROR")
            if not isinstance(response.get("result"), dict):
                raise ProbeError("RPC_RESULT_INVALID")
            self._drain_stderr()
            self.completed_ids.add(response_id)
            return response["result"]

    def _drain_stderr(self):
        try:
            self.selector.get_key(self.process.stderr)
        except KeyError:
            return
        while True:
            try:
                chunk = os.read(self.process.stderr.fileno(), 8192)
            except BlockingIOError:
                return
            if not chunk:
                self.selector.unregister(self.process.stderr)
                return
            self.stderr_bytes += len(chunk)
            if self.stderr_bytes > MAX_STDERR:
                raise ProbeError("RPC_STDERR_LIMIT")


def evaluate_config(result):
    config = result.get("config") if isinstance(result, dict) else None
    if not isinstance(config, dict) or not isinstance(config.get("agents"), dict):
        return item("BLOCKED", "CONFIG_FIELDS_UNKNOWN")
    expected = {"model": "gpt-6.1-sol", "model_reasoning_effort": "xhigh",
                "forced_login_method": "chatgpt"}
    expected_agents = {"enabled": False, "max_concurrent_threads_per_session": 2,
                       "default_subagent_model": "gpt-6.1-sol",
                       "default_subagent_reasoning_effort": "xhigh"}
    if any(k not in config for k in expected) or any(k not in config["agents"] for k in expected_agents):
        return item("BLOCKED", "CONFIG_FIELDS_UNKNOWN")
    if any(type(config[k]) is not type(v) for k, v in expected.items()) or any(type(config["agents"][k]) is not type(v) for k, v in expected_agents.items()):
        return item("BLOCKED", "CONFIG_FIELDS_UNKNOWN")
    if any(config[k] != v for k, v in expected.items()) or any(config["agents"][k] != v for k, v in expected_agents.items()):
        return item("FAIL", "CONFIG_POLICY_MISMATCH")
    if config.get("model_provider") not in (None, "openai"):
        return item("FAIL", "CONFIG_POLICY_MISMATCH")
    return item("PASS", "CONFIG_DEFAULTS_MATCH_DISABLED_PREPARATION")


def evaluate_account(result):
    account = result.get("account") if isinstance(result, dict) else None
    if not isinstance(account, dict) or not isinstance(account.get("type"), str):
        return item("BLOCKED", "ACCOUNT_UNKNOWN")
    if account["type"] != "chatgpt":
        return item("FAIL", "ACCOUNT_AUTH_METHOD_MISMATCH")
    if type(result.get("requiresOpenaiAuth")) is not bool:
        return item("BLOCKED", "ACCOUNT_UNKNOWN")
    if result["requiresOpenaiAuth"] is not True:
        return item("FAIL", "ACCOUNT_PROVIDER_MISMATCH")
    return item("PASS", "CHATGPT_ACCOUNT_OBSERVED")


def evaluate_models(pages):
    if not isinstance(pages, list) or not pages or len(pages) > MAX_MODEL_PAGES:
        return item("BLOCKED", "MODEL_CATALOG_UNKNOWN")
    target = None
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            return item("BLOCKED", "MODEL_CATALOG_UNKNOWN")
        for model in page["data"]:
            if not isinstance(model, dict):
                return item("BLOCKED", "MODEL_CATALOG_UNKNOWN")
            if model.get("model", model.get("id")) == "gpt-6.1-sol":
                target = model
    if target is None:
        return item("FAIL", "MODEL_UNAVAILABLE")
    efforts = target.get("supportedReasoningEfforts")
    if not isinstance(efforts, list) or not efforts or any(not isinstance(e, dict) or not isinstance(e.get("reasoningEffort"), str) for e in efforts):
        return item("BLOCKED", "MODEL_EFFORT_UNKNOWN")
    if not any(e["reasoningEffort"] == "xhigh" for e in efforts):
        return item("FAIL", "MODEL_EFFORT_UNAVAILABLE")
    return item("PASS", "MODEL_CATALOG_ADVERTISES_REQUESTED_OPTION")


def evaluate_limits(result, now=None):
    if not isinstance(result, dict):
        return item("BLOCKED", "QUOTA_FIELDS_UNKNOWN")
    buckets = result.get("rateLimitsByLimitId")
    if buckets is not None:
        if not isinstance(buckets, dict) or not buckets:
            return item("BLOCKED", "QUOTA_FIELDS_UNKNOWN")
        buckets = list(buckets.values())
    else:
        buckets = [result.get("rateLimits")]
    windows = 0
    now = time.time() if now is None else now
    for bucket in buckets:
        if not isinstance(bucket, dict):
            return item("BLOCKED", "QUOTA_FIELDS_UNKNOWN")
        reached = bucket.get("rateLimitReachedType")
        if reached is not None:
            return item("BLOCKED", "QUOTA_EXHAUSTED" if isinstance(reached, str) and reached else "QUOTA_FIELDS_UNKNOWN")
        bucket_windows = 0
        for key in ("primary", "secondary"):
            window = bucket.get(key)
            if window is None:
                continue
            if not isinstance(window, dict):
                return item("BLOCKED", "QUOTA_FIELDS_UNKNOWN")
            used, duration, reset = (window.get(k) for k in ("usedPercent", "windowDurationMins", "resetsAt"))
            if type(used) not in (int, float) or not 0 <= used <= 100 or type(duration) is not int or duration <= 0 or type(reset) is not int:
                return item("BLOCKED", "QUOTA_FIELDS_UNKNOWN")
            if used >= 100:
                return item("BLOCKED", "QUOTA_EXHAUSTED")
            if reset <= now:
                return item("BLOCKED", "QUOTA_SNAPSHOT_STALE")
            windows += 1
            bucket_windows += 1
        if not bucket_windows:
            return item("BLOCKED", "QUOTA_FIELDS_UNKNOWN")
    return item("PASS", "QUOTA_WINDOWS_AVAILABLE_AT_OBSERVATION") if windows else item("BLOCKED", "QUOTA_FIELDS_UNKNOWN")


def credit_gate(_snapshot):
    # Neither a balance nor a reset coupon proves paid-credit usage is disabled.
    return item("BLOCKED", "CREDIT_ENFORCEMENT_UNVERIFIED")


def read_allowed_snapshot(client):
    client.call("initialize", {"clientInfo": {"name": "codex_readiness_probe", "title": "Readiness probe", "version": "1.0.0"}})
    client.notify_initialized()
    config = client.call("config/read", {"includeLayers": False})
    account = client.call("account/read", {"refreshToken": False})
    verdict = evaluate_account(account)
    if verdict["status"] != "PASS":
        raise ProbeError(verdict["reason"])
    pages, seen, cursor = [], set(), None
    for _ in range(MAX_MODEL_PAGES):
        params = {"limit": 20, "includeHidden": False}
        if cursor is not None:
            params["cursor"] = cursor
        page = client.call("model/list", params)
        pages.append(page)
        if "nextCursor" not in page:
            raise ProbeError("MODEL_CURSOR_INVALID")
        cursor = page["nextCursor"]
        if cursor is None:
            break
        if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
            raise ProbeError("MODEL_CURSOR_INVALID")
        if cursor in seen:
            raise ProbeError("MODEL_CURSOR_REPEATED")
        seen.add(cursor)
    else:
        raise ProbeError("MODEL_PAGE_LIMIT")
    return {"config": config, "account": account, "models": pages,
            "limits": client.call("account/rateLimits/read")}


def _auth_stamp():
    root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    target = root / "auth.json"
    if root.is_symlink() or target.is_symlink():
        raise ProbeError("AUTH_STORE_UNSAFE")
    if not target.exists():
        return None
    info = target.stat()
    if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
        raise ProbeError("AUTH_STORE_UNSAFE")
    return hashlib.sha256(target.read_bytes()).digest()


def _version(command):
    with RpcClient([command, "--version"], total_timeout=3, request_timeout=3) as process:
        # Version output is not JSON. The same bounded pipe reader is used.
        raw = process._line(time.monotonic() + 3)
        if len(raw) > 4096:
            raise ProbeError("CLI_VERSION_INVALID")
        match = re.fullmatch(rb"codex(?:-cli)? ([0-9]+\.[0-9]+\.[0-9]+)(?:[-+][A-Za-z0-9.-]+)?", raw.strip())
        if not match:
            raise ProbeError("CLI_VERSION_INVALID")
        return match.group(1).decode("ascii")


def report(args):
    result = {"status": "BLOCKED", "observed_at_unix": int(time.time()),
        "evidence_source": "local_cli_app_server_metadata",
        "native_environment": "unverified", "checks": {
        "credit_enforcement": credit_gate(None),
        "role_overrides": item("BLOCKED", "ROLE_OVERRIDES_UNVERIFIED"),
        "aggregate_concurrency": item("BLOCKED", "AGGREGATE_CAP_UNVERIFIED")},
        "model_invocations": 0, "worker_started": False, "github_changed": False}
    checks = result["checks"]
    requested_command = getattr(args, "codex", None)
    command = shutil.which(requested_command or "codex")
    if not command:
        checks["cli"] = item("BLOCKED", "CLI_MISSING")
        return result
    expected_uid = getattr(args, "expected_uid", None)
    if expected_uid is None:
        checks["execution_account"] = item("BLOCKED", "EXECUTION_ACCOUNT_UNPINNED")
        return result
    if os.getuid() != expected_uid:
        checks["execution_account"] = item("FAIL", "EXECUTION_ACCOUNT_MISMATCH")
        result["status"] = "FAIL"
        return result
    checks["execution_account"] = item("PASS", "EXECUTION_ACCOUNT_MATCHES_REQUEST")
    if any(os.environ.get(key) for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL")):
        checks["auth"] = item("BLOCKED", "API_AUTH_ENV_PRESENT")
        return result
    before = None
    started = time.monotonic()
    try:
        before = _auth_stamp()
        version = _version(command)
        checks["cli"] = {**item("PASS", "CLI_VERSION_OBSERVED"), "version": version}
        worktree = getattr(args, "worktree", None)
        if worktree is not None and (not Path(worktree).is_dir() or Path(worktree).is_symlink()):
            raise ProbeError("WORKTREE_UNSAFE_OR_MISSING")
        timeout = max(0.01, 58 - (time.monotonic() - started))
        with RpcClient([command, "--strict-config", "app-server", "--listen", "stdio://"],
                       cwd=worktree, total_timeout=timeout) as client:
            snapshot = read_allowed_snapshot(client)
        checks.update({"configuration": evaluate_config(snapshot["config"]),
                       "auth": evaluate_account(snapshot["account"]),
                       "model_catalog": evaluate_models(snapshot["models"]),
                       "quota": evaluate_limits(snapshot["limits"])})
    except ProbeError as error:
        if error.code in ("ACCOUNT_AUTH_METHOD_MISMATCH", "ACCOUNT_PROVIDER_MISMATCH"):
            checks["auth"] = item("FAIL", error.code)
        else:
            checks["probe"] = item("BLOCKED", error.code)
    except (OSError, ValueError, TypeError, RecursionError, subprocess.SubprocessError):
        checks["probe"] = item("BLOCKED", "PROBE_ENVIRONMENT_OR_SCHEMA_ERROR")
    finally:
        try:
            unchanged = before == _auth_stamp()
            checks["auth_store_observation"] = item("BLOCKED", "AUTH_STORE_OBSERVATION_FILE_ONLY") if unchanged else item("FAIL", "AUTH_STORE_CHANGED")
        except (OSError, ProbeError):
            checks["auth_store_observation"] = item("BLOCKED", "AUTH_STORE_OBSERVATION_UNKNOWN")
    if any(check.get("status") == "FAIL" for check in checks.values()):
        result["status"] = "FAIL"
    return result


class SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        raise ProbeError("INVALID_ARGUMENTS")


def main():
    parser = SafeParser(description=__doc__)
    parser.add_argument("--codex")
    parser.add_argument("--worktree", type=Path)
    parser.add_argument("--expected-uid", type=int)
    try:
        output = report(parser.parse_args())
    except ProbeError as error:
        output = {"status": "BLOCKED", "reason": error.code,
                  "model_invocations": 0, "worker_started": False}
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
