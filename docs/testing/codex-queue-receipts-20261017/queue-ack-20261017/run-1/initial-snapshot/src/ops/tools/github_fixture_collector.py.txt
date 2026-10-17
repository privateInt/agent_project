"""Collect fixed diagnostic children, not PR code or trusted required checks.

Python 3.10+, Linux. No CLI, arbitrary execution, signing or network API.
Borrowed state FD and supplied digest do not authenticate their origins.
"""

import fcntl
import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import time

from src.ops.tools.github_check_audit import canonical_binding_digest


_MAX_ASSIGNMENT = 16384
_TARGET_KEYS = ("repository", "pr_number", "base_sha", "head_sha",
                "release_sha256", "plan_sha256")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_JOB = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_REASONS = frozenset({
    "ARGUMENTS_INVALID", "STATE_FD_INVALID", "ASSIGNMENT_FILE_INVALID",
    "ASSIGNMENT_HASH_MISMATCH", "ASSIGNMENT_INVALID", "REAL_EXECUTION_BLOCKED",
    "SPAWN_FAILED", "TIMEOUT", "OUTPUT_LIMIT_EXCEEDED", "IO_UNCONFIRMED",
    "CLEANUP_UNCONFIRMED", "UNEXPECTED_FAILURE", "EXIT_ZERO", "EXIT_NONZERO", "SIGNALED",
})
_PROFILES = {
    "ok": (
        "import os,stat\n"
        "assert stat.S_ISCHR(os.fstat(0).st_mode)\n"
        "assert os.read(0,1)==b''\n"
        "assert os.stat('/dev/null').st_rdev==os.fstat(0).st_rdev\n"
        "for name in os.listdir('/proc/self/fd'):\n"
        " fd=int(name)\n"
        " if fd<3: continue\n"
        " try: os.fstat(fd)\n"
        " except OSError: continue\n"
        " raise SystemExit(19)\n"
        "os.write(1,b'fixture-ok\\n')\n"
        "os.write(2,b'fixture-stderr\\n')\n"
    ),
    "nonzero": "import os\nos.write(1,b'PASS\\n')\nraise SystemExit(7)\n",
    "dual-output": (
        "import os\n"
        "for fd,data in ((1,b'O'*32768),(2,b'E'*32768)):\n"
        " while data:\n"
        "  n=os.write(fd,data)\n"
        "  data=data[n:]\n"
    ),
    "wait": "import time\ntime.sleep(10)\n",
    "closed-pipes-wait": "import os,time\nos.close(1)\nos.close(2)\ntime.sleep(10)\n",
    "signal": "import os,signal\nos.kill(os.getpid(),signal.SIGTERM)\n",
}


class _Failure(Exception):
    def __init__(self, reason):
        self.reason = reason


def _shape(value, keys):
    return (type(value) is dict and len(value) == len(keys)
            and all(type(key) is str for key in value) and set(value) == set(keys))


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _integer(value):
    if len(value) > 20:
        raise ValueError()
    return int(value)


def _nonfinite(value):
    raise ValueError()


def _assignment(raw):
    try:
        if raw.startswith(b"\xef\xbb\xbf"):
            raise ValueError()
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique,
                          parse_int=_integer, parse_constant=_nonfinite)
        stack = [(data, 1)]
        while stack:
            value, depth = stack.pop()
            if type(value) in (dict, list):
                if depth > 8:
                    raise ValueError()
                values = value.values() if type(value) is dict else value
                stack.extend((item, depth + 1) for item in values)
        if (not _shape(data, ("schema", "mode", "job_id", "target", "fixture_profile",
                              "timeout_ms", "max_stream_bytes"))
                or type(data["schema"]) is not int or data["schema"] != 1
                or type(data["mode"]) is not str or data["mode"] not in ("fixture", "head")
                or type(data["job_id"]) is not str or len(data["job_id"]) > 128
                or _JOB.fullmatch(data["job_id"]) is None
                or type(data["fixture_profile"]) is not str
                or data["fixture_profile"] not in _PROFILES
                or type(data["timeout_ms"]) is not int
                or not 100 <= data["timeout_ms"] <= 2000
                or type(data["max_stream_bytes"]) is not int
                or not 1 <= data["max_stream_bytes"] <= 65536
                or not _shape(data["target"], _TARGET_KEYS)
                or canonical_binding_digest(dict(data["target"], evidence_sha256="0" * 64)) is None):
            raise ValueError()
    except Exception:
        raise _Failure("ASSIGNMENT_INVALID") from None
    if data["mode"] == "head":
        raise _Failure("REAL_EXECUTION_BLOCKED")
    return data


def _load_assignment(state_dir_fd, expected_sha256):
    owned = []
    reason = "STATE_FD_INVALID"
    failed = None
    raw = b""
    try:
        directory = fcntl.fcntl(state_dir_fd, fcntl.F_DUPFD_CLOEXEC, 3)
        owned.append(directory)
        info = os.fstat(directory)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise _Failure(reason)
        reason = "ASSIGNMENT_FILE_INVALID"
        fd = os.open("assignment.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                     | os.O_CLOEXEC, dir_fd=directory)
        owned.append(fd)
        if fd < 3:
            reserved = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 3)
            owned.append(reserved)
            owned.remove(fd)
            try:
                os.close(fd)  # Never retry a failed close on its recycled number.
            except Exception:
                raise _Failure("CLEANUP_UNCONFIRMED") from None
            fd = reserved
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or not 1 <= info.st_size <= _MAX_ASSIGNMENT):
            raise _Failure(reason)
        while len(raw) <= _MAX_ASSIGNMENT:
            part = os.read(fd, min(4096, _MAX_ASSIGNMENT + 1 - len(raw)))
            if not part:
                break
            raw += part
        if not 1 <= len(raw) <= _MAX_ASSIGNMENT:
            raise _Failure(reason)
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise _Failure("ASSIGNMENT_HASH_MISMATCH")
    except _Failure as error:
        failed = error
    except Exception:
        failed = _Failure(reason)
    finally:
        for fd in owned:
            try:
                os.close(fd)
            except Exception:
                if failed is None:
                    failed = _Failure("CLEANUP_UNCONFIRMED")
    if failed is not None:
        raise failed
    return _assignment(raw)


def _report(reason, observed=None, verified=False):
    if type(reason) is not str or reason not in _REASONS:
        reason = "UNEXPECTED_FAILURE"
    observed = observed or {}
    stdout = observed.get("stdout", {})
    stderr = observed.get("stderr", {})
    outcome = observed.get("outcome", {})
    return {
        "scope": "FIXTURE_ASSIGNMENT_COLLECTION_ONLY",
        "status": "OBSERVED" if reason in ("EXIT_ZERO", "EXIT_NONZERO", "SIGNALED") else "BLOCKED",
        "reason": reason,
        "assignment_bytes_verified": verified,
        "collection_complete": outcome.get("collection_complete", False),
        "exit_zero_observed": outcome.get("exit_zero_observed", False),
        "returncode": outcome.get("returncode"),
        "child_reaped": outcome.get("child_reaped", False),
        "stdout_captured_bytes": stdout.get("captured_bytes", 0),
        "stderr_captured_bytes": stderr.get("captured_bytes", 0),
        "stdout_eof": stdout.get("eof", False),
        "stderr_eof": stderr.get("eof", False),
        "output_complete": outcome.get("output_complete", False),
        "output_limit_exceeded": stdout.get("limit_exceeded", False) or stderr.get("limit_exceeded", False),
        "timed_out": outcome.get("timed_out", False),
        "fixture_only": True,
        "head_code_executed": False,
        "input_origin_verified": False,
        "signer_roster_origin_verified": False,
        "issuer_environment_verified": False,
        "runtime_isolation_verified": False,
        "independent_review_verified": False,
        "trusted_gate_verified": False,
        "binding_verified": False,
        "native_ready": False,
        "merge_authorized": False,
    }


def _finish_child(child):
    cleanup_ok, reaped, forced, returncode = True, False, False, None
    try:
        if child.poll() is None:
            forced = True
            child.kill()
    except Exception:
        cleanup_ok = False
    try:
        returncode = child.wait(timeout=1)
        reaped = type(returncode) is int
        if not reaped:
            returncode = None
            cleanup_ok = False
    except Exception:
        cleanup_ok = False
    for pipe in (child.stdout, child.stderr):
        try:
            if not pipe.closed:
                pipe.close()
        except Exception:
            cleanup_ok = False
    return returncode, reaped, forced, cleanup_ok


def _observe_child(child, assignment, deadline):
    streams = {name: {"captured_bytes": 0, "observed_bytes": 0,
                      "sha256": hashlib.sha256(), "eof": False,
                      "limit_exceeded": False}
               for name in ("stdout", "stderr")}
    selector = None
    stop_reason = None
    cap = assignment["max_stream_bytes"]
    try:
        selector = selectors.DefaultSelector()
        for name in streams:
            pipe = getattr(child, name)
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        while selector.get_map() or child.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop_reason = "TIMEOUT"
                break
            if not selector.get_map():
                time.sleep(min(0.01, remaining))
                continue
            for key, mask in selector.select(min(0.05, remaining)):
                row = streams[key.data]
                try:
                    data = os.read(key.fd, min(8192, cap - row["captured_bytes"] + 1))
                except BlockingIOError:
                    continue
                if not data:
                    row["eof"] = True
                    selector.unregister(key.fileobj)
                    continue
                room = cap - row["captured_bytes"]
                retained = data[:room]
                row["sha256"].update(retained)
                row["captured_bytes"] += len(retained)
                row["observed_bytes"] += len(data)
                if len(data) > room:
                    row["limit_exceeded"] = True
                    stop_reason = "OUTPUT_LIMIT_EXCEEDED"
                    break
            if stop_reason is not None:
                break
    except Exception:
        stop_reason = "IO_UNCONFIRMED"
    finally:
        returncode, reaped, forced, cleanup_ok = _finish_child(child)
        if selector is not None:
            try:
                selector.close()
            except Exception:
                cleanup_ok = False
    output_complete = (stop_reason is None and all(row["eof"] for row in streams.values())
                       and reaped and cleanup_ok)
    collection_complete = output_complete and reaped and cleanup_ok
    exit_zero_observed = reaped and returncode == 0
    if stop_reason is None and not collection_complete:
        stop_reason = "CLEANUP_UNCONFIRMED"
    if stop_reason is None:
        stop_reason = "EXIT_ZERO" if returncode == 0 else "SIGNALED" if returncode < 0 else "EXIT_NONZERO"
    for row in streams.values():
        row["sha256"] = row["sha256"].hexdigest()
        row["prefix_only"] = not output_complete
    return {
        "outcome": {
            "reason": stop_reason, "returncode": returncode, "child_reaped": reaped,
            "collection_complete": collection_complete, "exit_zero_observed": exit_zero_observed,
            "output_complete": output_complete, "timed_out": stop_reason == "TIMEOUT",
            "forced_termination_requested": forced,
        },
        "stdout": streams["stdout"], "stderr": streams["stderr"],
    }


def collect_fixture_assignment(state_dir_fd, expected_assignment_sha256):
    """Load a pinned assignment and observe one fixed child; never run head code.

    Returns bytes/metadata for the caller to retain. No files or checks are
    written here, and all nine origin/trust/merge flags always remain false.
    """
    verified = False
    try:
        if (type(state_dir_fd) is not int or state_dir_fd < 0
                or type(expected_assignment_sha256) is not str
                or len(expected_assignment_sha256) != 64
                or _SHA256.fullmatch(expected_assignment_sha256) is None):
            raise _Failure("ARGUMENTS_INVALID")
        assignment = _load_assignment(state_dir_fd, expected_assignment_sha256)
        verified = True
        code = _PROFILES[assignment["fixture_profile"]]
        deadline = time.monotonic() + assignment["timeout_ms"] / 1000
        try:
            child = subprocess.Popen(
                ["/usr/bin/python3", "-I", "-B", "-c", code],
                cwd="/", env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0, shell=False, close_fds=True, pass_fds=(), start_new_session=True,
            )
        except Exception:
            raise _Failure("SPAWN_FAILED") from None
        try:
            observed = _observe_child(child, assignment, deadline)
        except Exception:
            # Also cover failures before observer setup. Already closed pipe
            # objects are skipped, and an already reaped Popen is never killed.
            _finish_child(child)
            raise
        artifact = _canonical({
            "schema": 1, "kind": "FIXTURE_DIAGNOSTIC",
            "job_id": assignment["job_id"], "target": assignment["target"],
            "assignment_sha256": expected_assignment_sha256,
            "fixture_profile": assignment["fixture_profile"],
            "fixture_code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
            "limits": {key: assignment[key] for key in ("timeout_ms", "max_stream_bytes")},
            **observed,
        })
        binding = dict(assignment["target"], evidence_sha256=hashlib.sha256(artifact).hexdigest())
        return {"report": _report(observed["outcome"]["reason"], observed, True),
                "artifact": artifact, "binding": binding}
    except _Failure as error:
        reason = error.reason
    except Exception:
        reason = "UNEXPECTED_FAILURE"
    return {"report": _report(reason, verified=verified), "artifact": None, "binding": None}
