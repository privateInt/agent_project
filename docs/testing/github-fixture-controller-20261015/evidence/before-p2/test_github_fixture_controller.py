"""C01-D29-v1: only the new controller boundary, never the old suites."""

import ast
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import selectors
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from src.ops.tools import github_fixture_collector as collector
from src.ops.tools import github_fixture_controller as controller
from src.ops.tools import github_fixture_ledger as ledger


ROOT = Path(__file__).resolve().parents[4]
OBSERVATIONS = []
TRUST = {
    "input_origin_verified", "signer_roster_origin_verified", "issuer_environment_verified",
    "runtime_isolation_verified", "independent_review_verified", "trusted_gate_verified",
    "binding_verified", "native_ready", "merge_authorized",
}
REPORT = TRUST | {
    "scope", "status", "reason", "control_state", "preparation_reason", "collection_reason",
    "artifact_storage_confirmed", "ledger_transaction_closed", "control_transaction_closed",
    "fixture_only", "head_code_executed",
}
REASONS = {
    "CONTROLLER_READY", "ARTIFACT_STORED", "PREPARATION_FAILED", "OUTCOME_UNKNOWN",
    "DATABASE_UNCONFIRMED", "ARGUMENTS_INVALID", "CONNECTION_INVALID", "SCHEMA_INVALID",
    "ASSIGNMENT_HASH_MISMATCH", "ASSIGNMENT_INVALID", "ASSIGNMENT_CONFLICT",
    "ASSIGNMENT_PIN_MISMATCH", "REAL_EXECUTION_BLOCKED", "JOB_NOT_FOUND", "STATE_CONFLICT",
}
PREPARATION = {
    "ARGUMENTS_INVALID", "STATE_FD_INVALID", "ASSIGNMENT_FILE_INVALID",
    "ASSIGNMENT_HASH_MISMATCH", "ASSIGNMENT_INVALID", "REAL_EXECUTION_BLOCKED",
}
PRIVATE = "PRIVATE_C01_EXCEPTION_MUST_NOT_APPEAR"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def assignment(profile="ok"):
    return {"schema": 1, "mode": "fixture", "job_id": "controller-job:1",
            "target": {"repository": "ExampleOrg/fixture", "pr_number": 1,
                       "base_sha": "a" * 40, "head_sha": "b" * 40,
                       "release_sha256": "c" * 64, "plan_sha256": "d" * 64},
            "fixture_profile": profile, "timeout_ms": 1000, "max_stream_bytes": 65536}


def connect(path, **kwargs):
    result = sqlite3.connect(path, isolation_level=None, timeout=1, **kwargs)
    result.execute("PRAGMA synchronous=FULL").close()
    result.execute("PRAGMA busy_timeout=1000").close()
    return result


def no_artifact(reason="SPAWN_FAILED"):
    # Coherent pinned-collector response; only the classification is injected.
    return {"report": collector._report(reason, verified=True), "artifact": None, "binding": None}


HARNESS = r"""import json,os,sqlite3,sys
sys.path.insert(0,sys.argv[1])
from pathlib import Path
from src.ops.tools import github_fixture_controller as c, github_fixture_collector as f
directory=Path(sys.argv[2])
def conn(name):
 x=sqlite3.connect(directory/name,isolation_level=None,timeout=1)
 x.execute('PRAGMA synchronous=FULL').close()
 x.execute('PRAGMA busy_timeout=1000').close()
 return x
l,k=conn('ledger.sqlite'),conn('control.sqlite')
raw=(directory/'state'/'assignment.json').read_bytes()
import hashlib
pin=hashlib.sha256(raw).hexdigest()
original=f.collect_fixture_assignment
def boundary(fd,digest):
 if sys.argv[3]=='crash':os._exit(70)
 marker=os.open(directory/'entries',os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_CLOEXEC,0o600)
 try:os.write(marker,b'COLLECT\n')
 finally:os.close(marker)
 return original(fd,digest)
f.collect_fixture_assignment=boundary
if sys.argv[3]=='race':
 os.write(1,b'READY\n')
 if os.read(0,1)!=b'G':raise SystemExit(71)
fd=os.open(directory/'state',os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)
try:result=c.run_fixture_job(l,k,fd,pin,raw)
finally:os.close(fd)
os.write(1,json.dumps(result['report'],separators=(',',':')).encode('ascii'))
l.close();k.close()
"""


class FixtureControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="controller-c01-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.pairs = []

    def make(self, profile="ok", initialized=True):
        directory = self.directory / str(len(self.pairs))
        directory.mkdir(mode=0o700)
        state = directory / "state"
        state.mkdir(mode=0o700)
        value = assignment(profile)
        raw = canonical(value)
        path = state / "assignment.json"
        path.write_bytes(raw)
        path.chmod(0o600)
        pair = SimpleNamespace(directory=directory, state=state, raw=raw, value=value,
                               pin=sha(raw), job=value["job_id"],
                               l=connect(directory / "ledger.sqlite"),
                               c=connect(directory / "control.sqlite"),
                               fd=os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
        self.pairs.append(pair)
        self.addCleanup(os.close, pair.fd)
        self.addCleanup(self.close, pair)
        if initialized:
            self.assertEqual(ledger.initialize_fixture_ledger(pair.l)["report"]["reason"], "LEDGER_READY")
            self.call("initialize_fixture_controller", pair.c, reason="CONTROLLER_READY")
        return pair

    def close(self, pair):
        for connection in (pair.l, pair.c):
            try:
                connection.close()
            except sqlite3.ProgrammingError:
                pass

    def reopen(self, pair):
        self.close(pair)
        pair.l = connect(pair.directory / "ledger.sqlite")
        pair.c = connect(pair.directory / "control.sqlite")

    def rows(self, pair):
        return (pair.l.execute("SELECT * FROM fixture_jobs ORDER BY job_id").fetchall(),
                pair.c.execute("SELECT * FROM fixture_controller_jobs ORDER BY job_id").fetchall())

    def run_job(self, pair, reason=None, **changes):
        values = {"ledger_connection": pair.l, "control_connection": pair.c,
                  "state_dir_fd": pair.fd, "expected_assignment_sha256": pair.pin,
                  "assignment_bytes": pair.raw}
        values.update(changes)
        return self.call("run_fixture_job", **values, reason=reason)

    def observe(self, pair, reason=None, pin=None):
        return self.call("read_fixture_controller_job", pair.l, pair.c, pair.job,
                         pair.pin if pin is None else pin, reason=reason)

    def call(self, name, *args, reason=None, **kwargs):
        # kwargs are used by run_job; centralize all report assertions above.
        return self._checked_call(name, args, kwargs, reason)

    def _checked_call(self, name, args, kwargs, reason):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = getattr(controller, name)(*args, **kwargs)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        self.assertIs(type(result), dict)
        self.assertEqual(set(result), {"report", "record", "artifact"})
        report = result["report"]
        self.assertEqual(set(report), REPORT)
        self.assertEqual(report["scope"], "FIXTURE_CONTROLLER_ONLY")
        self.assertIn(report["reason"], REASONS)
        self.assertEqual(report["status"], "OBSERVED" if report["reason"] in
                         ("CONTROLLER_READY", "ARTIFACT_STORED") else "BLOCKED")
        self.assertIn(report["control_state"], (None, "CLAIMED", "PREPARATION_FAILED"))
        self.assertTrue(report["preparation_reason"] is None or report["preparation_reason"] in PREPARATION)
        self.assertTrue(report["collection_reason"] is None or report["collection_reason"] in collector._REASONS)
        for key in TRUST | {"artifact_storage_confirmed", "ledger_transaction_closed",
                            "control_transaction_closed", "fixture_only", "head_code_executed"}:
            self.assertIs(type(report[key]), bool, key)
        self.assertTrue(report["fixture_only"])
        for key in TRUST | {"head_code_executed"}:
            self.assertFalse(report[key], key)
        self.assertEqual(report["artifact_storage_confirmed"], report["reason"] == "ARTIFACT_STORED")
        self.assertNotIn(PRIVATE.encode(), canonical(report))
        self.assertNotIn(self.temp.name.encode(), canonical(report))
        if reason is not None:
            self.assertEqual(report["reason"], reason)
        if report["artifact_storage_confirmed"]:
            row = result["record"]
            self.assertEqual(row["revision"], 1)
            self.assertIs(type(result["artifact"]), bytes)
            self.assertEqual(row["artifact_bytes"], result["artifact"])
            self.assertEqual(row["artifact_sha256"], sha(result["artifact"]))
            target = json.loads(row["assignment_bytes"])["target"]
            self.assertEqual(row["binding"], dict(target, evidence_sha256=sha(result["artifact"])))
        return result

    def child(self, pair, action):
        return subprocess.Popen([sys.executable, "-I", "-B", "-c", HARNESS,
                                 str(ROOT), str(pair.directory), action],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})

    def test_c01_schema_and_explicit_initialization(self):
        pair = self.make(initialized=False)
        with mock.patch.object(collector, "collect_fixture_assignment") as collect:
            self.run_job(pair, "SCHEMA_INVALID")
            self.observe(pair, "SCHEMA_INVALID")
            self.assertEqual(collect.call_count, 0)
        for connection in (pair.l, pair.c):
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master").fetchall(), [])
        self.assertEqual(ledger.initialize_fixture_ledger(pair.l)["report"]["reason"], "LEDGER_READY")
        self.call("initialize_fixture_controller", pair.c, reason="CONTROLLER_READY")
        self.assertEqual(pair.c.execute("PRAGMA application_id").fetchone()[0], 1095779907)
        self.assertEqual(pair.c.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(pair.c.execute("SELECT type,name FROM sqlite_master").fetchall(),
                         [("table", "fixture_controller_jobs")])
        self.assertEqual([r[1:3] for r in pair.c.execute("PRAGMA table_info(fixture_controller_jobs)")],
                         [("job_id", "TEXT"), ("assignment_sha256", "TEXT"), ("state", "TEXT"), ("preparation_reason", "TEXT")])
        with self.assertRaises(sqlite3.IntegrityError):
            pair.c.execute("INSERT INTO fixture_controller_jobs VALUES (?,?,'PREPARATION_FAILED',NULL)", (pair.job, pair.pin))
        with mock.patch.object(collector, "collect_fixture_assignment", return_value=no_artifact()):
            self.run_job(pair, "OUTCOME_UNKNOWN")
        before = self.rows(pair)
        self.call("initialize_fixture_controller", pair.c, reason="CONTROLLER_READY")
        self.assertEqual(self.rows(pair), before)
        self.reopen(pair)
        self.observe(pair, "OUTCOME_UNKNOWN")
        self.assertEqual(self.rows(pair), before)

    def test_c02_real_collection_and_cached_readback(self):
        for profile, code in (("ok", 0), ("nonzero", 7)):
            with self.subTest(profile=profile):
                pair, captured = self.make(profile), []
                real = collector.collect_fixture_assignment
                def boundary(fd, pin):
                    self.assertFalse(pair.l.in_transaction)
                    self.assertFalse(pair.c.in_transaction)
                    self.assertEqual(pair.c.execute("SELECT state FROM fixture_controller_jobs").fetchone(), ("CLAIMED",))
                    result = real(fd, pin)
                    captured.append(result)
                    return result
                with mock.patch.object(collector, "collect_fixture_assignment", side_effect=boundary) as collect:
                    result = self.run_job(pair, "ARTIFACT_STORED")
                    self.assertEqual(result["artifact"], captured[0]["artifact"])
                    self.assertEqual(json.loads(result["artifact"])["outcome"]["returncode"], code)
                    before = self.rows(pair)
                    self.reopen(pair)
                    self.assertEqual(self.observe(pair, "ARTIFACT_STORED")["artifact"], result["artifact"])
                    self.assertEqual(self.run_job(pair, "ARTIFACT_STORED")["artifact"], result["artifact"])
                    self.assertEqual(collect.call_count, 1)
                    self.assertEqual(self.rows(pair), before)
                OBSERVATIONS.append({"case": "C02", "profile": profile, "actual_returncode": code,
                                     "collector_calls": 1, "artifact_sha256": sha(result["artifact"]), "reopen_matches": True})

    def test_c03_real_preparation_failure_is_terminal(self):
        pair = self.make()
        (pair.state / "assignment.json").unlink()
        original_fd = os.fstat(pair.fd)
        with mock.patch.object(collector, "collect_fixture_assignment", wraps=collector.collect_fixture_assignment) as collect, mock.patch.object(collector.subprocess, "Popen", wraps=subprocess.Popen) as popen:
            result = self.run_job(pair, "PREPARATION_FAILED")
            self.assertIsNone(result["artifact"])
            self.assertEqual(result["report"]["preparation_reason"], "ASSIGNMENT_FILE_INVALID")
            before = self.rows(pair)
            self.assertEqual(before[0][0][3:], (0, None, None))
            self.reopen(pair)
            (pair.state / "assignment.json").write_bytes(pair.raw)
            (pair.state / "assignment.json").chmod(0o600)
            self.observe(pair, "PREPARATION_FAILED")
            self.run_job(pair, "PREPARATION_FAILED")
            self.assertEqual(self.rows(pair), before)
            self.assertEqual((collect.call_count, popen.call_count), (1, 0))
        self.assertEqual(os.fstat(pair.fd).st_ino, original_fd.st_ino)
        OBSERVATIONS.append({"case": "C03", "actual_missing_file": True, "collector_calls": 1, "child_spawns": 0, "terminal_after_repair": True})

    def unknown_case(self, response):
        pair = self.make()
        kwargs = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
        with mock.patch.object(collector, "collect_fixture_assignment", **kwargs) as collect:
            result = self.run_job(pair, "OUTCOME_UNKNOWN")
            self.assertEqual(result["report"]["control_state"], "CLAIMED")
            self.assertIsNone(result["report"]["preparation_reason"])
            before = self.rows(pair)
            self.reopen(pair)
            self.observe(pair, "OUTCOME_UNKNOWN")
            self.run_job(pair, "OUTCOME_UNKNOWN")
            self.assertEqual(collect.call_count, 1)
            self.assertEqual(self.rows(pair), before)

    def test_c04_spawn_failed_is_unknown(self):
        self.unknown_case(no_artifact("SPAWN_FAILED"))

    def test_c04_other_unknowns(self):
        for reason in ("IO_UNCONFIRMED", "CLEANUP_UNCONFIRMED", "UNEXPECTED_FAILURE"):
            with self.subTest(reason=reason):
                self.unknown_case(no_artifact(reason))
        contradictory = no_artifact("ASSIGNMENT_FILE_INVALID")
        contradictory["report"]["child_reaped"] = True
        wrong_binding = no_artifact()
        wrong_binding["binding"] = {"unexpected": True}
        unknown = no_artifact()
        unknown["report"]["reason"] = PRIVATE
        for index, response in enumerate((RuntimeError(PRIVATE), {}, contradictory, wrong_binding, unknown)):
            with self.subTest(malformed=index):
                self.unknown_case(response)

    def test_c05_crash_after_claim_never_recollects(self):
        pair = self.make()
        child = self.child(pair, "crash")
        try:
            out, err = child.communicate(timeout=5)
            self.assertEqual((child.returncode, out, err), (70, b"", b""))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
        self.reopen(pair)
        result = self.observe(pair, "OUTCOME_UNKNOWN")
        self.assertEqual(result["report"]["control_state"], "CLAIMED")
        before = self.rows(pair)
        with mock.patch.object(collector, "collect_fixture_assignment", side_effect=AssertionError("second collection")) as collect:
            self.run_job(pair, "OUTCOME_UNKNOWN")
            self.assertEqual(collect.call_count, 0)
        self.assertEqual(self.rows(pair), before)
        OBSERVATIONS.append({"case": "C05", "actual_process_exit": 70, "durable_claim": True, "repeat_collector_calls": 0})

    def test_c06_real_process_claim_race(self):
        pair = self.make()
        children = [self.child(pair, "race") for _ in range(2)]
        try:
            for child in children:
                with selectors.DefaultSelector() as ready:
                    ready.register(child.stdout, selectors.EVENT_READ)
                    self.assertTrue(ready.select(5), "fixed harness readiness timeout")
                    self.assertEqual(child.stdout.readline(), b"READY\n")
            for child in children:
                child.stdin.write(b"G")
                child.stdin.close()
                child.stdin = None
            reports = []
            for child in children:
                out, err = child.communicate(timeout=5)
                self.assertEqual((child.returncode, err), (0, b""))
                reports.append(json.loads(out))
            self.assertEqual((pair.directory / "entries").read_bytes(), b"COLLECT\n")
            self.assertTrue(any(r["reason"] == "ARTIFACT_STORED" for r in reports))
            self.assertTrue(all(r["reason"] in ("ARTIFACT_STORED", "OUTCOME_UNKNOWN") for r in reports))
            self.reopen(pair)
            result = self.observe(pair, "ARTIFACT_STORED")
            self.assertEqual(len(self.rows(pair)[1]), 1)
            OBSERVATIONS.append({"case": "C06", "actual_processes": 2, "collector_entries": 1,
                                 "reasons": [r["reason"] for r in reports], "artifact_sha256": sha(result["artifact"])})
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=2)
                for pipe in (child.stdin, child.stdout, child.stderr):
                    if pipe is not None:
                        pipe.close()

    @contextlib.contextmanager
    def control_fault(self, stage, after_commit=False, rollback_fail=False):
        original_stage = getattr(controller, stage)
        real_commit = controller._commit
        def lost(connection):
            if after_commit:
                real_commit(connection)
            raise sqlite3.OperationalError(PRIVATE)
        def wrapped(*args):
            with mock.patch.object(controller, "_commit", side_effect=lost):
                if rollback_fail:
                    with mock.patch.object(controller, "_rollback", side_effect=sqlite3.OperationalError(PRIVATE)):
                        return original_stage(*args)
                return original_stage(*args)
        with mock.patch.object(controller, stage, side_effect=wrapped):
            yield

    def test_c07_claim_commit_uncertainty(self):
        for after, rollback_fail in ((False, False), (True, False), (False, True)):
            with self.subTest(after_commit=after, rollback_fail=rollback_fail):
                pair = self.make()
                with self.control_fault("_claim", after, rollback_fail), mock.patch.object(collector, "collect_fixture_assignment") as collect:
                    result = self.run_job(pair, "DATABASE_UNCONFIRMED")
                    self.assertEqual(collect.call_count, 0)
                self.assertEqual(result["report"]["control_transaction_closed"], not rollback_fail)
                self.assertTrue(result["report"]["ledger_transaction_closed"])
                if rollback_fail:
                    self.assertTrue(pair.c.in_transaction)
                    pair.c.rollback()  # Only the test cleans up its own failed transaction.
                self.reopen(pair)
                observed = self.observe(pair, "OUTCOME_UNKNOWN")
                self.assertEqual(observed["report"]["control_state"], "CLAIMED" if after else None)
                if after:
                    with mock.patch.object(collector, "collect_fixture_assignment") as collect:
                        self.run_job(pair, "OUTCOME_UNKNOWN")
                        self.assertEqual(collect.call_count, 0)
                OBSERVATIONS.append({"case": "C07", "injected": True, "after_commit": after,
                                     "rollback_fail": rollback_fail, "collector_calls": 0,
                                     "initial": "DATABASE_UNCONFIRMED", "observed_state": observed["report"]["control_state"]})

    def test_c08_preparation_commit_uncertainty(self):
        for after in (False, True):
            with self.subTest(after_commit=after):
                pair = self.make()
                (pair.state / "assignment.json").unlink()
                with self.control_fault("_fail_preparation", after), mock.patch.object(collector, "collect_fixture_assignment", wraps=collector.collect_fixture_assignment) as collect:
                    self.run_job(pair, "DATABASE_UNCONFIRMED")
                    self.reopen(pair)
                    expected = "PREPARATION_FAILED" if after else "OUTCOME_UNKNOWN"
                    self.observe(pair, expected)
                    (pair.state / "assignment.json").write_bytes(pair.raw)
                    (pair.state / "assignment.json").chmod(0o600)
                    self.run_job(pair, expected)
                    self.assertEqual(collect.call_count, 1)
                OBSERVATIONS.append({"case": "C08", "injected_commit_loss": True, "after_commit": after,
                                     "initial": "DATABASE_UNCONFIRMED", "observed": expected})

    def test_c09_artifact_commit_uncertainty_retains_original_bytes(self):
        for after in (False, True):
            with self.subTest(after_commit=after):
                pair, captured = self.make(), []
                real_collect = collector.collect_fixture_assignment
                real_record, real_commit = ledger.record_fixture_artifact, ledger._commit
                def collect(fd, pin):
                    result = real_collect(fd, pin)
                    captured.append(result["artifact"])
                    return result
                def lost(connection):
                    if after:
                        real_commit(connection)
                    raise sqlite3.OperationalError(PRIVATE)
                def record(*args):
                    with mock.patch.object(ledger, "_commit", side_effect=lost):
                        return real_record(*args)
                with mock.patch.object(collector, "collect_fixture_assignment", side_effect=collect) as count:
                    with mock.patch.object(ledger, "record_fixture_artifact", side_effect=record):
                        initial = self.run_job(pair, "DATABASE_UNCONFIRMED")
                    self.assertEqual(initial["artifact"], captured[0])
                    self.assertFalse(initial["report"]["artifact_storage_confirmed"])
                    self.reopen(pair)
                    expected = "ARTIFACT_STORED" if after else "OUTCOME_UNKNOWN"
                    observed = self.observe(pair, expected)
                    self.run_job(pair, expected)
                    self.assertEqual(count.call_count, 1)
                    if after:
                        self.assertEqual(observed["artifact"], captured[0])
                OBSERVATIONS.append({"case": "C09", "injected_commit_loss": True, "after_commit": after,
                                     "initial": "DATABASE_UNCONFIRMED", "observed": expected,
                                     "original_bytes_retained": True, "artifact_sha256": sha(captured[0])})

    def test_c10_conflicting_and_orphan_states_preserve_data(self):
        pair = self.make()
        with mock.patch.object(collector, "collect_fixture_assignment", return_value=no_artifact()):
            self.run_job(pair, "OUTCOME_UNKNOWN")
        before = self.rows(pair)
        other = pair.raw + b" "
        with mock.patch.object(collector, "collect_fixture_assignment") as collect:
            self.run_job(pair, "ASSIGNMENT_CONFLICT", assignment_bytes=other, expected_assignment_sha256=sha(other))
            rejected = self.observe(pair, "ASSIGNMENT_PIN_MISMATCH", pin="e" * 64)
            self.assertIsNone(rejected["record"])
            self.assertIsNone(rejected["artifact"])
            self.assertEqual(collect.call_count, 0)
        self.assertEqual(self.rows(pair), before)
        pair.c.execute("UPDATE fixture_controller_jobs SET assignment_sha256=?", ("e" * 64,)).close()
        before = self.rows(pair)
        with mock.patch.object(collector, "collect_fixture_assignment") as collect:
            self.observe(pair, "STATE_CONFLICT")
            self.run_job(pair, "STATE_CONFLICT")
            self.assertEqual(collect.call_count, 0)
        self.assertEqual(self.rows(pair), before)
        orphan = self.make()
        orphan.c.execute("INSERT INTO fixture_controller_jobs VALUES (?,?,'CLAIMED',NULL)", (orphan.job, orphan.pin)).close()
        before = self.rows(orphan)
        with mock.patch.object(collector, "collect_fixture_assignment") as collect:
            self.observe(orphan, "STATE_CONFLICT")
            self.run_job(orphan, "STATE_CONFLICT")
            self.assertEqual(collect.call_count, 0)
        self.assertEqual(self.rows(orphan), before)
        failed = self.make()
        (failed.state / "assignment.json").unlink()
        self.run_job(failed, "PREPARATION_FAILED")
        (failed.state / "assignment.json").write_bytes(failed.raw)
        (failed.state / "assignment.json").chmod(0o600)
        injected = collector.collect_fixture_assignment(failed.fd, failed.pin)["artifact"]
        self.assertEqual(ledger.record_fixture_artifact(failed.l, failed.job, failed.pin, 0, injected)["report"]["reason"], "ARTIFACT_RECORDED")
        before = self.rows(failed)
        with mock.patch.object(collector, "collect_fixture_assignment") as collect:
            rejected = self.observe(failed, "STATE_CONFLICT")
            self.run_job(failed, "STATE_CONFLICT")
            self.assertEqual(collect.call_count, 0)
            self.assertIsNone(rejected["artifact"])
        self.assertEqual(self.rows(failed), before)

    def test_c11_connections_schema_busy_and_corruption(self):
        for role in ("l", "c"):
            with self.subTest(caller_transaction=role):
                pair = self.make()
                target = getattr(pair, role)
                target.execute("BEGIN IMMEDIATE").close()
                traces = ([], [])
                pair.l.set_trace_callback(traces[0].append)
                pair.c.set_trace_callback(traces[1].append)
                try:
                    with mock.patch.object(collector, "collect_fixture_assignment") as collect:
                        result = self.run_job(pair, "CONNECTION_INVALID")
                        self.observe(pair, "CONNECTION_INVALID")
                        self.assertEqual(collect.call_count, 0)
                    self.assertEqual(traces, ([], []))
                    self.assertTrue(target.in_transaction)
                    self.assertFalse(result["report"]["ledger_transaction_closed" if role == "l" else "control_transaction_closed"])
                finally:
                    pair.l.set_trace_callback(None)
                    pair.c.set_trace_callback(None)
                    target.rollback()
        for role, sql in (("c", "PRAGMA busy_timeout=1001"), ("l", "PRAGMA synchronous=OFF"),
                          ("c", "PRAGMA journal_mode=WAL"), ("l", "ATTACH DATABASE ':memory:' AS other"),
                          ("c", "CREATE TEMP TABLE other(x)"), ("c", "CREATE TABLE extra(x)"),
                          ("l", "PRAGMA user_version=2")):
            with self.subTest(role=role, sql=sql):
                pair = self.make()
                target = getattr(pair, role)
                target.execute(sql).close()
                before = self.rows(pair)
                with mock.patch.object(collector, "collect_fixture_assignment") as collect:
                    result = self.run_job(pair)
                    self.assertIn(result["report"]["reason"], ("CONNECTION_INVALID", "SCHEMA_INVALID"))
                    self.assertEqual(collect.call_count, 0)
                self.assertEqual(self.rows(pair), before)
        pair = self.make()
        self.run_job(pair, "ARGUMENTS_INVALID", control_connection=pair.l)
        closed = connect(pair.directory / "closed.sqlite")
        closed.close()
        self.run_job(pair, "CONNECTION_INVALID", control_connection=closed)
        class Derived(sqlite3.Connection):
            pass
        derived = connect(pair.directory / "derived.sqlite", factory=Derived)
        try:
            self.run_job(pair, "CONNECTION_INVALID", control_connection=derived)
        finally:
            derived.close()
        for column, value in (("assignment_sha256", "x" * 20000), ("preparation_reason", "x" * 20000)):
            pair = self.make()
            with mock.patch.object(collector, "collect_fixture_assignment", return_value=no_artifact()):
                self.run_job(pair, "OUTCOME_UNKNOWN")
            pair.c.execute("PRAGMA ignore_check_constraints=ON").close()
            pair.c.execute("UPDATE fixture_controller_jobs SET " + column + "=?", (value,)).close()
            pair.c.execute("PRAGMA ignore_check_constraints=OFF").close()
            before = self.rows(pair)
            self.observe(pair, "STATE_CONFLICT")
            self.assertEqual(self.rows(pair), before)
        pair = self.make()
        pair.c.execute("PRAGMA busy_timeout=100").close()
        locker = connect(pair.directory / "control.sqlite")
        locker.execute("BEGIN IMMEDIATE").close()
        try:
            started = time.monotonic()
            with mock.patch.object(collector, "collect_fixture_assignment") as collect:
                self.run_job(pair, "DATABASE_UNCONFIRMED")
                self.assertEqual(collect.call_count, 0)
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 0.09)
            self.assertLess(elapsed, 2)
            self.assertTrue(locker.in_transaction)
            OBSERVATIONS.append({"case": "C11", "actual_busy_timeout_ms": 100, "elapsed_seconds": elapsed, "collector_calls": 0})
        finally:
            locker.rollback()
            locker.close()

    def test_c12_input_readonly_output_and_static_boundary(self):
        for raw, pin, expected in ((b"", "a" * 64, "ARGUMENTS_INVALID"),
                                   (canonical(assignment()), "e" * 64, "ASSIGNMENT_HASH_MISMATCH"),
                                   (b"{" + PRIVATE.encode(), "0" * 64, "ASSIGNMENT_HASH_MISMATCH")):
            pair = self.make()
            with mock.patch.object(collector, "collect_fixture_assignment") as collect:
                self.run_job(pair, expected, assignment_bytes=raw, expected_assignment_sha256=pin)
                self.assertEqual(collect.call_count, 0)
        pair = self.make()
        head = assignment()
        head["mode"] = "head"
        raw = canonical(head)
        with mock.patch.object(collector, "collect_fixture_assignment") as collect:
            self.run_job(pair, "REAL_EXECUTION_BLOCKED", assignment_bytes=raw, expected_assignment_sha256=sha(raw))
            self.assertEqual(collect.call_count, 0)
        with mock.patch.object(collector, "collect_fixture_assignment", return_value=no_artifact()):
            self.run_job(pair, "OUTCOME_UNKNOWN")
        before = self.rows(pair)
        database_bytes = [(pair.directory / name).read_bytes() for name in ("ledger.sqlite", "control.sqlite")]
        traces = ([], [])
        pair.l.set_trace_callback(traces[0].append)
        pair.c.set_trace_callback(traces[1].append)
        try:
            with mock.patch.object(collector, "collect_fixture_assignment") as collect:
                self.observe(pair, "OUTCOME_UNKNOWN")
                self.assertEqual(collect.call_count, 0)
        finally:
            pair.l.set_trace_callback(None)
            pair.c.set_trace_callback(None)
        self.assertEqual(self.rows(pair), before)
        self.assertEqual([(pair.directory / name).read_bytes() for name in ("ledger.sqlite", "control.sqlite")], database_bytes)
        for trace in traces:
            self.assertFalse(any(sql.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"} for sql in trace))
        path = ROOT / "src/ops/tools/github_fixture_controller.py"
        tree = ast.parse(path.read_text(), feature_version=(3, 10))
        self.assertEqual({node.name for node in tree.body if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")},
                         {"initialize_fixture_controller", "run_fixture_job", "read_fixture_controller_job"})
        imports = {node.names[0].name for node in tree.body if isinstance(node, ast.Import)}
        self.assertEqual(imports, {"hashlib", "json", "sqlite3"})
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        self.assertFalse(any(isinstance(n.func, ast.Name) and n.func.id in {"print", "open", "eval", "exec"} for n in calls))
        self.assertFalse(any(isinstance(n.func, ast.Attribute) and n.func.attr in {"connect", "Popen", "system", "remove", "unlink"} for n in calls))
        self.assertIn("LIMIT 2", controller._SCHEMA_SQL)
        self.assertIn("length(preparation_reason)<=64", controller._ROW_SQL)
        self.assertIn("WHERE job_id=?", controller._ROW_SQL)


if __name__ == "__main__":
    unittest.main()
