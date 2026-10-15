import ast
import contextlib
import errno
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from src.ops.tools import github_fixture_collector as q


ROOT = Path(__file__).resolve().parents[4]
PLAN_SHA = "f0056e49a04eca4608f65eaae3872f31aca41505dc06a311043754b2104a9ea3"
CANONICAL_SHA = "d2b8deb1b9a652d396c63f42d2c581af51c7c19aa9bfa7785f2e740580b3515e"
PRIVATE = "PRIVATE_FIXTURE_SENTINEL_do_not_echo"
TARGET = {
    "repository": "ExampleOrg/fixture",
    "pr_number": 1,
    "base_sha": "a" * 40,
    "head_sha": "b" * 40,
    "release_sha256": "c" * 64,
    "plan_sha256": "d" * 64,
}
TRUST = {
    "input_origin_verified", "signer_roster_origin_verified",
    "issuer_environment_verified", "runtime_isolation_verified",
    "independent_review_verified", "trusted_gate_verified", "binding_verified",
    "native_ready", "merge_authorized",
}
REPORT_KEYS = TRUST | {
    "scope", "status", "reason", "assignment_bytes_verified",
    "collection_complete", "exit_zero_observed", "returncode", "child_reaped",
    "stdout_captured_bytes", "stderr_captured_bytes", "stdout_eof", "stderr_eof",
    "output_complete", "output_limit_exceeded", "timed_out", "fixture_only",
    "head_code_executed",
}
REASONS = {
    "ARGUMENTS_INVALID", "STATE_FD_INVALID", "ASSIGNMENT_FILE_INVALID",
    "ASSIGNMENT_HASH_MISMATCH", "ASSIGNMENT_INVALID", "REAL_EXECUTION_BLOCKED",
    "SPAWN_FAILED", "TIMEOUT", "OUTPUT_LIMIT_EXCEEDED", "IO_UNCONFIRMED",
    "CLEANUP_UNCONFIRMED", "UNEXPECTED_FAILURE", "EXIT_ZERO", "EXIT_NONZERO",
    "SIGNALED",
}
ARTIFACT_KEYS = {
    "schema", "kind", "job_id", "target", "assignment_sha256", "fixture_profile",
    "fixture_code_sha256", "limits", "outcome", "stdout", "stderr",
}
OUTCOME_KEYS = {
    "reason", "returncode", "child_reaped", "collection_complete",
    "exit_zero_observed", "output_complete", "timed_out", "forced_termination_requested",
}
STREAM_KEYS = {"captured_bytes", "observed_bytes", "sha256", "prefix_only", "eof", "limit_exceeded"}
OBSERVATIONS = []


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def assignment(profile="ok", cap=65536, timeout=1000, **changes):
    result = {
        "schema": 1, "mode": "fixture", "job_id": "fixture-job:1",
        "target": dict(TARGET), "fixture_profile": profile,
        "timeout_ms": timeout, "max_stream_bytes": cap,
    }
    result.update(changes)
    return result


class Proxy:
    def __init__(self, wrapped, **overrides):
        self.wrapped = wrapped
        self.overrides = overrides

    def __getattr__(self, name):
        if name in self.overrides:
            return self.overrides[name]
        return getattr(self.wrapped, name)


@contextlib.contextmanager
def state(value=None, raw=None, mutate=None):
    value = assignment() if value is None else value
    raw = canonical(value) if raw is None else raw
    with tempfile.TemporaryDirectory(prefix="fixture-collector-") as directory:
        os.chmod(directory, 0o700)
        path = Path(directory) / "assignment.json"
        path.write_bytes(raw)
        os.chmod(path, 0o600)
        if mutate:
            mutate(path)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            yield fd, digest(raw), value, path
        finally:
            os.close(fd)


@contextlib.contextmanager
def no_spawn():
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("unexpected fixture spawn")

    with mock.patch.object(q, "subprocess", Proxy(subprocess, Popen=forbidden)):
        yield calls


@contextlib.contextmanager
def real_spawns():
    calls = []
    real = subprocess.Popen

    def spawn(*args, **kwargs):
        child = real(*args, **kwargs)
        calls.append((args, kwargs, child))
        return child

    with mock.patch.object(q, "subprocess", Proxy(subprocess, Popen=spawn)):
        yield calls


class FixtureCollectorTests(unittest.TestCase):
    def invoke(self, fd, expected):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = q.collect_fixture_assignment(fd, expected)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertIs(type(result), dict)
        self.assertEqual(set(result), {"report", "artifact", "binding"})
        report = result["report"]
        self.assertIs(type(report), dict)
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual(report["scope"], "FIXTURE_ASSIGNMENT_COLLECTION_ONLY")
        self.assertIn(report["status"], {"OBSERVED", "BLOCKED"})
        self.assertIn(report["reason"], REASONS)
        for name in REPORT_KEYS - {"scope", "status", "reason", "returncode", "stdout_captured_bytes", "stderr_captured_bytes"}:
            self.assertIs(type(report[name]), bool, name)
        for name in ("stdout_captured_bytes", "stderr_captured_bytes"):
            self.assertIs(type(report[name]), int)
            self.assertGreaterEqual(report[name], 0)
            self.assertLessEqual(report[name], 65536)
        self.assertTrue(report["returncode"] is None or type(report["returncode"]) is int)
        self.assertTrue(report["fixture_only"])
        self.assertFalse(report["head_code_executed"])
        for name in TRUST:
            self.assertFalse(report[name], name)
        public = canonical(report)
        self.assertNotIn(PRIVATE.encode(), public)
        for hidden in ("job_id", "repository", "assignment_sha256", "fixture_code_sha256"):
            self.assertNotIn(hidden.encode(), public)
        if result["artifact"] is None:
            self.assertIsNone(result["binding"])
        else:
            self.assertIs(type(result["artifact"]), bytes)
            self.assertIs(type(result["binding"]), dict)
        return result

    def artifact(self, result, value, expected, code=None):
        raw = result["artifact"]
        self.assertIs(type(raw), bytes)
        artifact = json.loads(raw)
        self.assertEqual(raw, canonical(artifact))
        self.assertEqual(set(artifact), ARTIFACT_KEYS)
        self.assertIs(type(artifact["schema"]), int)
        self.assertEqual(artifact["schema"], 1)
        self.assertEqual(artifact["kind"], "FIXTURE_DIAGNOSTIC")
        self.assertEqual(artifact["job_id"], value["job_id"])
        self.assertEqual(artifact["target"], value["target"])
        self.assertEqual(artifact["assignment_sha256"], expected)
        self.assertEqual(artifact["fixture_profile"], value["fixture_profile"])
        self.assertEqual(artifact["limits"], {name: value[name] for name in ("timeout_ms", "max_stream_bytes")})
        self.assertRegex(artifact["fixture_code_sha256"], r"^[0-9a-f]{64}$")
        if code is not None:
            self.assertEqual(artifact["fixture_code_sha256"], digest(code.encode("utf-8")))
        self.assertEqual(set(artifact["outcome"]), OUTCOME_KEYS)
        for name in OUTCOME_KEYS - {"forced_termination_requested"}:
            self.assertEqual(artifact["outcome"][name], result["report"][name], name)
        self.assertIs(type(artifact["outcome"]["forced_termination_requested"]), bool)
        for label in ("stdout", "stderr"):
            stream = artifact[label]
            self.assertEqual(set(stream), STREAM_KEYS)
            self.assertIs(type(stream["captured_bytes"]), int)
            self.assertIs(type(stream["observed_bytes"]), int)
            self.assertLessEqual(stream["captured_bytes"], value["max_stream_bytes"])
            self.assertGreaterEqual(stream["observed_bytes"], stream["captured_bytes"])
            self.assertLessEqual(stream["observed_bytes"], value["max_stream_bytes"] + 1)
            self.assertEqual(stream["captured_bytes"], result["report"][label + "_captured_bytes"])
            self.assertEqual(stream["eof"], result["report"][label + "_eof"])
            self.assertRegex(stream["sha256"], r"^[0-9a-f]{64}$")
            for name in ("prefix_only", "eof", "limit_exceeded"):
                self.assertIs(type(stream[name]), bool)
        self.assertEqual(result["binding"], {**value["target"], "evidence_sha256": digest(raw)})
        self.assertNotIn(b'"evidence_sha256"', raw)
        self.assertNotIn(b'"binding"', raw)
        self.assertNotIn(b'"receipt"', raw)
        self.assertNotIn(PRIVATE.encode(), raw)
        return artifact

    def assert_reaped(self, calls):
        self.assertEqual(len(calls), 1)
        child = calls[0][2]
        self.assertIsNotNone(child.returncode)
        with self.assertRaises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
        for pipe in (child.stdout, child.stderr):
            self.assertTrue(pipe.closed)

    def test_01_real_ok_end_to_end(self):
        with state() as (fd, expected, value, path), real_spawns() as calls:
            before = (os.fstat(fd), fcntl.fcntl(fd, fcntl.F_GETFD), os.lseek(fd, 0, os.SEEK_CUR))
            result = self.invoke(fd, expected)
            self.assertEqual(result["report"]["status"], "OBSERVED")
            self.assertEqual(result["report"]["reason"], "EXIT_ZERO")
            for name in ("assignment_bytes_verified", "collection_complete", "exit_zero_observed", "child_reaped", "output_complete", "stdout_eof", "stderr_eof"):
                self.assertTrue(result["report"][name], name)
            self.assertEqual(result["report"]["returncode"], 0)
            self.assertFalse(result["report"]["output_limit_exceeded"])
            self.assertFalse(result["report"]["timed_out"])
            a = self.artifact(result, value, expected, calls[0][0][0][-1])
            for label, raw in (("stdout", b"fixture-ok\n"), ("stderr", b"fixture-stderr\n")):
                self.assertEqual(a[label]["captured_bytes"], len(raw))
                self.assertEqual(a[label]["observed_bytes"], len(raw))
                self.assertEqual(a[label]["sha256"], digest(raw))
                self.assertFalse(a[label]["prefix_only"])
            self.assertEqual(before, (os.fstat(fd), fcntl.fcntl(fd, fcntl.F_GETFD), os.lseek(fd, 0, os.SEEK_CUR)))
            self.assertNotIn(str(path).encode(), canonical(result["report"]))
            self.assert_reaped(calls)

    def test_02_real_nonzero_pass_text_is_not_exit_zero(self):
        with state(assignment("nonzero")) as (fd, expected, value, _), real_spawns() as calls:
            result = self.invoke(fd, expected)
            r = result["report"]
            self.assertEqual(r["status"], "OBSERVED")
            self.assertEqual(r["reason"], "EXIT_NONZERO")
            self.assertEqual(r["returncode"], 7)
            self.assertFalse(r["exit_zero_observed"])
            self.assertTrue(r["collection_complete"])
            self.assertTrue(r["output_complete"])
            a = self.artifact(result, value, expected)
            self.assertEqual(a["stdout"]["sha256"], digest(b"PASS\n"))
            self.assertEqual(a["stdout"]["captured_bytes"], 5)
            self.assertEqual(a["stderr"]["sha256"], digest(b""))
            self.assert_reaped(calls)

    def test_03_real_signaled_exit(self):
        with state(assignment("signal")) as (fd, expected, value, _), real_spawns() as calls:
            result = self.invoke(fd, expected)
            r = result["report"]
            self.assertEqual((r["status"], r["reason"], r["returncode"]), ("OBSERVED", "SIGNALED", -signal.SIGTERM))
            self.assertTrue(r["collection_complete"])
            self.assertFalse(r["exit_zero_observed"])
            self.artifact(result, value, expected)
            self.assert_reaped(calls)

    def test_04_real_dual_stream_and_exact_cap(self):
        for cap in (65536, 32768):
            with self.subTest(cap=cap), state(assignment("dual-output", cap)) as (fd, expected, value, _), real_spawns() as calls:
                result = self.invoke(fd, expected)
                self.assertEqual(result["report"]["reason"], "EXIT_ZERO")
                self.assertTrue(result["report"]["output_complete"])
                a = self.artifact(result, value, expected)
                for label, byte in (("stdout", b"O"), ("stderr", b"E")):
                    self.assertEqual(a[label]["captured_bytes"], 32768)
                    self.assertEqual(a[label]["observed_bytes"], 32768)
                    self.assertEqual(a[label]["sha256"], digest(byte * 32768))
                    self.assertTrue(a[label]["eof"])
                    self.assertFalse(a[label]["prefix_only"])
                    self.assertFalse(a[label]["limit_exceeded"])
                self.assert_reaped(calls)

    def test_05_real_overflow_is_not_complete_output(self):
        with state(assignment("dual-output", cap=64)) as (fd, expected, value, _), real_spawns() as calls:
            result = self.invoke(fd, expected)
            r = result["report"]
            self.assertEqual((r["status"], r["reason"]), ("BLOCKED", "OUTPUT_LIMIT_EXCEEDED"))
            self.assertFalse(r["output_complete"])
            self.assertFalse(r["collection_complete"])
            self.assertTrue(r["output_limit_exceeded"])
            self.assertTrue(r["child_reaped"])
            a = self.artifact(result, value, expected)
            self.assertTrue(any(a[label]["limit_exceeded"] for label in ("stdout", "stderr")))
            for label, byte in (("stdout", b"O"), ("stderr", b"E")):
                self.assertTrue(a[label]["prefix_only"])
                self.assertEqual(a[label]["sha256"], digest(byte * a[label]["captured_bytes"]))
                if a[label]["limit_exceeded"]:
                    self.assertEqual(a[label]["captured_bytes"], 64)
                    self.assertEqual(a[label]["observed_bytes"], 65)
            self.assert_reaped(calls)

    def test_06_real_timeout_and_eof_before_exit(self):
        for profile in ("wait", "closed-pipes-wait"):
            with self.subTest(profile=profile), state(assignment(profile, timeout=150)) as (fd, expected, value, _), real_spawns() as calls:
                started = time.monotonic()
                result = self.invoke(fd, expected)
                elapsed = time.monotonic() - started
                OBSERVATIONS.append({"case": "R06", "profile": profile, "elapsed_seconds": elapsed, "returncode": result["report"]["returncode"], "child_reaped": result["report"]["child_reaped"], "stdout_eof": result["report"]["stdout_eof"], "stderr_eof": result["report"]["stderr_eof"]})
                self.assertLess(elapsed, 3.0)
                self.assertGreaterEqual(elapsed, 0.10)
                r = result["report"]
                self.assertEqual((r["status"], r["reason"]), ("BLOCKED", "TIMEOUT"))
                self.assertTrue(r["timed_out"])
                self.assertTrue(r["child_reaped"])
                self.assertFalse(r["output_complete"])
                self.assertFalse(r["collection_complete"])
                if profile == "closed-pipes-wait":
                    self.assertTrue(r["stdout_eof"])
                    self.assertTrue(r["stderr_eof"])
                a = self.artifact(result, value, expected)
                self.assertTrue(a["outcome"]["forced_termination_requested"])
                self.assertTrue(a["stdout"]["prefix_only"])
                self.assertTrue(a["stderr"]["prefix_only"])
                self.assert_reaped(calls)

    def test_07_fd_arguments_and_directory_pins(self):
        with state() as (fd, expected, _, path), no_spawn() as calls:
            for badfd, badhash in ((True, expected), (-1, expected), ("0", expected), (fd, None), (fd, "A" * 64), (fd, "0" * 63)):
                with self.subTest(fd=type(badfd).__name__, hash_type=type(badhash).__name__):
                    self.assertEqual(self.invoke(badfd, badhash)["report"]["reason"], "ARGUMENTS_INVALID")
            filefd = os.open(path, os.O_RDONLY)
            try:
                self.assertEqual(self.invoke(filefd, expected)["report"]["reason"], "STATE_FD_INVALID")
            finally:
                os.close(filefd)
            os.chmod(path.parent, 0o750)
            self.assertEqual(self.invoke(fd, expected)["report"]["reason"], "STATE_FD_INVALID")
            os.chmod(path.parent, 0o700)
            real_fstat = os.fstat

            def wrong_owner(number):
                value = real_fstat(number)
                return types.SimpleNamespace(st_mode=value.st_mode, st_uid=os.geteuid() + 1, st_size=value.st_size, st_nlink=value.st_nlink)

            with mock.patch.object(q, "os", Proxy(os, fstat=wrong_owner)):
                self.assertEqual(self.invoke(fd, expected)["report"]["reason"], "STATE_FD_INVALID")
            os.fstat(fd)
            self.assertEqual(calls, [])

    def test_08_real_assignment_file_denials(self):
        def replace(path, kind):
            if kind == "mode":
                os.chmod(path, 0o640)
            elif kind == "link":
                os.link(path, path.parent / "second-public-link")
            elif kind in {"empty", "oversize"}:
                path.write_bytes(b"" if kind == "empty" else b" " * 16385)
            else:
                path.unlink()
                if kind == "symlink":
                    path.symlink_to("absent-public-target")
                elif kind == "fifo":
                    os.mkfifo(path, 0o600)

        for kind in ("missing", "symlink", "fifo", "link", "mode", "empty", "oversize"):
            with self.subTest(kind=kind), state(mutate=lambda path: replace(path, kind)) as (fd, expected, _, _), no_spawn() as calls:
                started = time.monotonic()
                self.assertEqual(self.invoke(fd, expected)["report"]["reason"], "ASSIGNMENT_FILE_INVALID")
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertEqual(calls, [])
        with state() as (fd, expected, _, _), no_spawn() as calls:
            real_fstat = os.fstat

            def wrong_file_owner(number):
                value = real_fstat(number)
                if stat.S_ISREG(value.st_mode):
                    return types.SimpleNamespace(st_mode=value.st_mode, st_uid=os.geteuid() + 1, st_size=value.st_size, st_nlink=value.st_nlink)
                return value

            with mock.patch.object(q, "os", Proxy(os, fstat=wrong_file_owner)):
                self.assertEqual(self.invoke(fd, expected)["report"]["reason"], "ASSIGNMENT_FILE_INVALID")
            self.assertEqual(calls, [])

    def test_09_hash_strict_json_shape_and_limits(self):
        with state() as (fd, expected, _, _), no_spawn() as calls:
            wrong = ("0" if expected[0] != "0" else "1") + expected[1:]
            self.assertEqual(self.invoke(fd, wrong)["report"]["reason"], "ASSIGNMENT_HASH_MISMATCH")
            self.assertEqual(calls, [])
        raw_ok = canonical(assignment())
        raw_bad = [b"\xef\xbb\xbf" + raw_ok, b"\xff", b"{}", b"[1]", b"NaN", b"Infinity", b"-Infinity", b"{" + b'"schema":1,' + raw_ok[1:], b'{"x":' + b"[" * 9 + b"0" + b"]" * 9 + b"}", b'{"x":123456789012345678901}']
        invalid = [assignment(schema=True), assignment(schema=2), assignment(job_id=""), assignment(job_id="x" * 129), assignment(job_id="bad space"), assignment(job_id="é"), assignment(timeout_ms=True), assignment(timeout_ms=99), assignment(timeout_ms=2001), assignment(max_stream_bytes=True), assignment(max_stream_bytes=0), assignment(max_stream_bytes=65537)]
        for key in TARGET:
            changed = dict(TARGET)
            changed.pop(key)
            invalid.append(assignment(target=changed))
        invalid.extend([assignment(target={**TARGET, "extra": 1}), assignment(target={**TARGET, "pr_number": True}), assignment(target={**TARGET, "head_sha": "B" * 40}), assignment(target={**TARGET, "repository": "bad repo/x"})])
        raw_bad.extend(canonical(value) for value in invalid)
        for raw in raw_bad:
            with self.subTest(raw_digest=digest(raw)), state(raw=raw) as (fd, expected, _, _), no_spawn() as calls:
                self.assertEqual(self.invoke(fd, expected)["report"]["reason"], "ASSIGNMENT_INVALID")
                self.assertEqual(calls, [])
        for value in (assignment(timeout=100), assignment(timeout=2000), assignment(cap=1), assignment(cap=65536)):
            with self.subTest(limits=(value["timeout_ms"], value["max_stream_bytes"])), state(value) as (fd, expected, _, _):
                failed = mock.Mock(side_effect=OSError(errno.ENOENT, PRIVATE))
                with mock.patch.object(q, "subprocess", Proxy(subprocess, Popen=failed)):
                    result = self.invoke(fd, expected)
                self.assertEqual(result["report"]["reason"], "SPAWN_FAILED")
                self.assertTrue(result["report"]["assignment_bytes_verified"])
                self.assertEqual(failed.call_count, 1)
        with state(raw=raw_ok.ljust(16384, b" ")) as (fd, expected, _, _):
            failed = mock.Mock(side_effect=OSError(errno.ENOENT, PRIVATE))
            with mock.patch.object(q, "subprocess", Proxy(subprocess, Popen=failed)):
                self.assertEqual(self.invoke(fd, expected)["report"]["reason"], "SPAWN_FAILED")
            self.assertEqual(failed.call_count, 1)

    def test_10_no_head_or_arbitrary_launcher(self):
        values = [(assignment(mode="head"), "REAL_EXECUTION_BLOCKED")]
        values.extend((value, "ASSIGNMENT_INVALID") for value in (assignment(mode="other"), assignment(fixture_profile="head"), assignment(argv=[PRIVATE]), assignment(env={"X": PRIVATE}), assignment(code=PRIVATE), assignment(launcher=PRIVATE)))
        for value, reason in values:
            with self.subTest(reason=reason, fields=sorted(value)), state(value) as (fd, expected, _, _), no_spawn() as calls:
                result = self.invoke(fd, expected)
                self.assertEqual(result["report"]["reason"], reason)
                self.assertIsNone(result["artifact"])
                self.assertEqual(calls, [])

    def test_11_real_fixed_spawn_noninheritance_and_low_fds(self):
        with state() as (fd, expected, value, path), real_spawns() as calls:
            extra = os.open(path, os.O_RDONLY)
            old = os.get_inheritable(fd)
            os.set_inheritable(fd, True)
            os.set_inheritable(extra, True)
            try:
                result = self.invoke(fd, expected)
                self.assertEqual(result["report"]["reason"], "EXIT_ZERO")
                args, kwargs, _ = calls[0]
                self.assertEqual(len(args), 1)
                self.assertEqual(args[0][0:4], ["/usr/bin/python3", "-I", "-B", "-c"])
                self.assertEqual(len(args[0]), 5)
                self.assertEqual(kwargs["cwd"], "/")
                self.assertEqual(kwargs["env"], {"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                self.assertEqual(kwargs["stdout"], subprocess.PIPE)
                self.assertEqual(kwargs["stderr"], subprocess.PIPE)
                self.assertEqual(kwargs["bufsize"], 0)
                self.assertIs(kwargs["shell"], False)
                self.assertIs(kwargs["close_fds"], True)
                self.assertEqual(kwargs["pass_fds"], ())
                self.assertIs(kwargs["start_new_session"], True)
                self.artifact(result, value, expected, args[0][-1])
                os.fstat(fd)
                os.fstat(extra)
                self.assertTrue(os.get_inheritable(fd))
                self.assertTrue(os.get_inheritable(extra))
                self.assert_reaped(calls)
            finally:
                os.set_inheritable(fd, old)
                os.close(extra)
        harness = """import json,os,sys
sys.path.insert(0,sys.argv[1])
from src.ops.tools import github_fixture_collector as q
out=int(sys.argv[4])
for number in (0,1,2):
    os.close(number)
fd=os.open(sys.argv[2],os.O_RDONLY|os.O_DIRECTORY)
try:
    value=q.collect_fixture_assignment(fd,sys.argv[3])
    os.fstat(fd)
    data={'fd':fd,'report':value['report']}
except BaseException:
    data={'harness_error':True}
os.write(out,json.dumps(data).encode('ascii'))
os._exit(0)
"""
        with state() as (_, expected, _, path):
            readfd, writefd = os.pipe2(os.O_CLOEXEC)
            try:
                completed = subprocess.run([sys.executable, "-I", "-B", "-c", harness, str(ROOT), str(path.parent), expected, str(writefd)], pass_fds=(writefd,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=5, check=False)
                os.close(writefd)
                writefd = None
                data = os.read(readfd, 16384)
                self.assertEqual(completed.returncode, 0)
                self.assertEqual(completed.stderr, b"")
                result = json.loads(data)
                self.assertEqual(set(result), {"fd", "report"})
                self.assertEqual(result["fd"], 0)
                self.assertEqual(result["report"]["reason"], "EXIT_ZERO")
                self.assertTrue(result["report"]["child_reaped"])
                self.assertTrue(result["report"]["collection_complete"])
            finally:
                if writefd is not None:
                    os.close(writefd)
                os.close(readfd)

    def test_12_fault_cleanup_and_no_close_retry(self):
        with state() as (fd, expected, _, _):
            failed = mock.Mock(side_effect=OSError(errno.ENOENT, PRIVATE))
            with mock.patch.object(q, "subprocess", Proxy(subprocess, Popen=failed)):
                result = self.invoke(fd, expected)
            self.assertEqual(result["report"]["reason"], "SPAWN_FAILED")
            self.assertIsNone(result["artifact"])
            self.assertEqual(failed.call_count, 1)
        for fault in ("read", "selector", "clock", "observer"):
            profile = "signal" if fault == "read" else "wait"
            with self.subTest(fault=fault), state(assignment(profile, timeout=150)) as (fd, expected, value, _), real_spawns() as calls:
                with contextlib.ExitStack() as patches:
                    if fault == "read":
                        real_read = os.read

                        def fail_pipe_read(number, count):
                            if stat.S_ISFIFO(os.fstat(number).st_mode):
                                raise OSError(errno.EIO, PRIVATE)
                            return real_read(number, count)

                        patches.enter_context(mock.patch.object(q, "os", Proxy(os, read=fail_pipe_read)))
                    elif fault == "selector":
                        patches.enter_context(mock.patch.object(q, "selectors", Proxy(q.selectors, DefaultSelector=mock.Mock(side_effect=OSError(errno.EIO, PRIVATE)))))
                    elif fault == "clock":
                        counter = [0]

                        def bad_clock():
                            counter[0] += 1
                            if counter[0] > 1:
                                raise OSError(errno.EIO, PRIVATE)
                            return time.monotonic()

                        patches.enter_context(mock.patch.object(q, "time", Proxy(time, monotonic=bad_clock)))
                    else:
                        patches.enter_context(mock.patch.object(q, "_observe_child", side_effect=RuntimeError(PRIVATE)))
                    result = self.invoke(fd, expected)
                self.assertEqual(result["report"]["reason"], "UNEXPECTED_FAILURE" if fault == "observer" else "IO_UNCONFIRMED")
                self.assertFalse(result["report"]["collection_complete"])
                self.assert_reaped(calls)
                if fault != "observer":
                    self.assertTrue(result["report"]["child_reaped"])
                    self.artifact(result, value, expected)
        with state() as (fd, expected, _, _), no_spawn() as calls:
            closes = []
            real_close = os.close

            def fail_one_close(number):
                self.assertNotEqual(number, fd)
                closes.append(number)
                real_close(number)
                if len(closes) == 1:
                    raise OSError(errno.EIO, PRIVATE)

            with mock.patch.object(q, "os", Proxy(os, close=fail_one_close)):
                result = self.invoke(fd, expected)
            self.assertEqual(result["report"]["reason"], "CLEANUP_UNCONFIRMED")
            self.assertGreaterEqual(len(closes), 2)
            self.assertEqual(len(closes), len(set(closes)))
            os.fstat(fd)
            self.assertEqual(calls, [])
            with mock.patch.object(q, "fcntl", Proxy(fcntl, fcntl=mock.Mock(side_effect=OSError(errno.EMFILE, PRIVATE)))):
                self.assertEqual(self.invoke(fd, expected)["report"]["reason"], "STATE_FD_INVALID")

        class ClosingPipe:
            def __init__(self, fail=False):
                readfd, writefd = os.pipe2(os.O_CLOEXEC)
                os.close(writefd)
                self.pipe = io.FileIO(readfd, "rb", closefd=True)
                self.fail = fail
                self.closes = 0

            @property
            def closed(self):
                return self.pipe.closed

            def fileno(self):
                return self.pipe.fileno()

            def close(self):
                self.closes += 1
                self.pipe.close()
                if self.fail:
                    raise OSError(errno.EIO, PRIVATE)

        class FakeChild:
            def __init__(self, fault):
                self.stdout = ClosingPipe()
                self.stderr = ClosingPipe(fault == "close")
                self.fault = fault
                self.returncode = 0 if fault == "close" else None
                self.kills = 0
                self.waits = []

            def poll(self):
                return self.returncode

            def kill(self):
                self.kills += 1
                if self.fault == "kill":
                    raise OSError(errno.EPERM, PRIVATE)
                self.returncode = -signal.SIGKILL

            def wait(self, timeout):
                self.waits.append(timeout)
                if self.fault == "wait":
                    raise subprocess.TimeoutExpired(PRIVATE, timeout)
                if self.returncode is None:
                    self.returncode = -signal.SIGKILL
                return self.returncode

        for fault in ("kill", "wait", "close"):
            child = FakeChild(fault)
            with self.subTest(cleanup_fault=fault), state() as (fd, expected, value, _):
                with contextlib.ExitStack() as patches:
                    spawn = mock.Mock(return_value=child)
                    patches.enter_context(mock.patch.object(q, "subprocess", Proxy(subprocess, Popen=spawn)))
                    if fault != "close":
                        patches.enter_context(mock.patch.object(q, "selectors", Proxy(q.selectors, DefaultSelector=mock.Mock(side_effect=OSError(errno.EIO, PRIVATE)))))
                    result = self.invoke(fd, expected)
                self.assertEqual(spawn.call_count, 1)
                self.assertEqual(result["report"]["reason"], "CLEANUP_UNCONFIRMED" if fault == "close" else "IO_UNCONFIRMED")
                self.assertFalse(result["report"]["collection_complete"])
                self.assertFalse(result["report"]["output_complete"])
                self.assertEqual(result["report"]["child_reaped"], fault != "wait")
                self.assertEqual(child.kills, 0 if fault == "close" else 1)
                self.assertEqual(child.waits, [1])
                self.assertEqual((child.stdout.closes, child.stderr.closes), (1, 1))
                self.assertTrue(child.stdout.closed)
                self.assertTrue(child.stderr.closed)
                artifact = self.artifact(result, value, expected)
                self.assertTrue(artifact["stdout"]["prefix_only"])
                self.assertTrue(artifact["stderr"]["prefix_only"])

    def test_13_canonical_artifact_and_non_circular_binding(self):
        values = [assignment(), assignment(job_id="fixture-job:2"), assignment(target={**TARGET, "base_sha": "e" * 40})]
        hashes = []
        for value in values:
            with self.subTest(job=value["job_id"], base=value["target"]["base_sha"]), state(value) as (fd, expected, _, _), real_spawns() as calls:
                before = canonical(value)
                result = self.invoke(fd, expected)
                a = self.artifact(result, value, expected, calls[0][0][0][-1])
                self.assertEqual(canonical(value), before)
                self.assertNotIn(b'"evidence_sha256"', canonical(a["target"]))
                self.assertEqual(set(result["binding"]), set(TARGET) | {"evidence_sha256"})
                hashes.append(result["binding"]["evidence_sha256"])
                self.assert_reaped(calls)
        self.assertEqual(len(set(hashes)), len(hashes))

    def test_14_sanitized_errors_and_narrow_fixture_claims(self):
        class HookInt(int):
            def __index__(self):
                raise AssertionError(PRIVATE)

        class HookStr(str):
            def __str__(self):
                raise AssertionError(PRIVATE)

        with state() as (fd, expected, _, _), no_spawn() as calls:
            for badfd, badhash in ((HookInt(fd), expected), (fd, HookStr(expected)), (fd, PRIVATE)):
                result = self.invoke(badfd, badhash)
                self.assertEqual(result["report"]["reason"], "ARGUMENTS_INVALID")
                self.assertEqual(calls, [])
            with mock.patch.object(q, "_load_assignment", side_effect=RuntimeError(PRIVATE)):
                result = self.invoke(fd, expected)
            self.assertEqual(result["report"]["reason"], "UNEXPECTED_FAILURE")
            self.assertIsNone(result["artifact"])
            self.assertEqual(calls, [])

    def test_15_syntax_import_boundaries_and_preserved_validator(self):
        path = ROOT / "src/ops/tools/github_fixture_collector.py"
        raw = path.read_bytes()
        tree = ast.parse(raw, feature_version=(3, 10))
        self.assertEqual(digest((ROOT / "docs/codex/github-fixture-collector-test-plan.md").read_bytes()), PLAN_SHA)
        self.assertEqual(digest((ROOT / "src/ops/tools/github_check_audit.py").read_bytes()), CANONICAL_SHA)
        old_tests = {
            "test_github_check_audit.py": "fcc0b85600114a578f55b8e075bc2519d4b71d900e5ddcd73faaf81b7e5539c5",
            "test_github_evidence_binding.py": "9bb7a99002451f3422e3db7fe130bafacac39d294d1facc371b340535ca01f41",
            "test_github_signed_receipts.py": "cc3e38cab9ae70b0cca5b4b3f791e9a9d2e1425298c382aacf35bcc70e9c7c71",
        }
        for name, expected in old_tests.items():
            self.assertEqual(digest((ROOT / "src/ops/tools/tests" / name).read_bytes()), expected)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, {"eval", "exec", "print", "input"})
                if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                    receiver, name = node.func.value.id, node.func.attr
                    if receiver == "os":
                        self.assertNotIn(name, {"write", "remove", "unlink", "rename", "replace", "chmod", "chown", "system", "popen", "execv", "execve"})
                    if receiver == "subprocess":
                        self.assertEqual(name, "Popen")
        self.assertTrue(imported <= {"errno", "fcntl", "hashlib", "json", "os", "re", "selectors", "signal", "stat", "subprocess", "time", "src", "typing"}, imported)
        self.assertFalse(any(isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__" for node in tree.body))
        completed = subprocess.run([sys.executable, "-I", "-B", "-c", "import sys;sys.path.insert(0,sys.argv[1]);from src.ops.tools import github_fixture_collector", str(ROOT)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False)
        self.assertEqual((completed.returncode, completed.stdout, completed.stderr), (0, b"", b""))


if __name__ == "__main__":
    unittest.main()
