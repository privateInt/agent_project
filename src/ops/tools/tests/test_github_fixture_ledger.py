import base64
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from src.ops.tools import github_fixture_collector as collector
from src.ops.tools import github_fixture_ledger as q


ROOT = Path(__file__).resolve().parents[4]
PRIVATE = "PRIVATE_LEDGER_SENTINEL_do_not_echo"
TARGET = {
    "repository": "ExampleOrg/fixture", "pr_number": 1,
    "base_sha": "a" * 40, "head_sha": "b" * 40,
    "release_sha256": "c" * 64, "plan_sha256": "d" * 64,
}
EXPECTED_DDL = (
    "CREATE TABLE fixture_jobs (job_id TEXT NOT NULL PRIMARY KEY, assignment BLOB NOT NULL, "
    "assignment_sha256 TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision IN (0,1)), "
    "artifact BLOB, artifact_sha256 TEXT, CHECK ((revision=0 AND artifact IS NULL AND "
    "artifact_sha256 IS NULL) OR (revision=1 AND artifact IS NOT NULL AND "
    "artifact_sha256 IS NOT NULL))) WITHOUT ROWID"
)
TRUST = {
    "input_origin_verified", "signer_roster_origin_verified",
    "issuer_environment_verified", "runtime_isolation_verified",
    "independent_review_verified", "trusted_gate_verified", "binding_verified",
    "native_ready", "merge_authorized",
}
REPORT_KEYS = TRUST | {
    "scope", "status", "reason", "operation_confirmed",
    "assignment_digest_verified", "artifact_digest_verified", "revision",
    "transaction_closed", "fixture_only", "head_code_executed",
}
RECORD_KEYS = {"job_id", "revision", "assignment_bytes", "assignment_sha256", "artifact_bytes", "artifact_sha256", "binding"}
NORMAL = {"LEDGER_READY", "ASSIGNED", "ASSIGNMENT_REPLAYED", "ARTIFACT_RECORDED", "ARTIFACT_REPLAYED", "JOB_OBSERVED"}
BLOCKED = {
    "ARGUMENTS_INVALID", "CONNECTION_INVALID", "SCHEMA_INVALID", "ASSIGNMENT_INVALID",
    "ASSIGNMENT_HASH_MISMATCH", "REAL_EXECUTION_BLOCKED", "JOB_NOT_FOUND",
    "ASSIGNMENT_CONFLICT", "ASSIGNMENT_PIN_MISMATCH", "REVISION_CONFLICT",
    "ARTIFACT_INVALID", "ARTIFACT_CONFLICT", "ROW_INVALID", "DATABASE_UNCONFIRMED",
}
OBSERVATIONS = []


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def assignment(profile="ok", **changes):
    value = {"schema": 1, "mode": "fixture", "job_id": "ledger-job:1", "target": dict(TARGET), "fixture_profile": profile, "timeout_ms": 1000, "max_stream_bytes": 65536}
    value.update(changes)
    return value


def artifact(value, tag=b"a", kind="complete"):
    def stream(raw, eof=True, prefix=False, exceeded=False, observed=None):
        return {"captured_bytes": len(raw), "observed_bytes": len(raw) if observed is None else observed, "sha256": sha(raw), "prefix_only": prefix, "eof": eof, "limit_exceeded": exceeded}

    stdout, stderr = stream(tag), stream(b"e")
    outcome = {"reason": "EXIT_ZERO", "returncode": 0, "child_reaped": True, "collection_complete": True, "exit_zero_observed": True, "output_complete": True, "timed_out": False, "forced_termination_requested": False}
    if kind in ("nonzero", "signal"):
        outcome.update(reason="EXIT_NONZERO" if kind == "nonzero" else "SIGNALED", returncode=7 if kind == "nonzero" else -signal.SIGTERM, exit_zero_observed=False)
    elif kind in ("timeout", "overflow", "io", "cleanup", "unreaped"):
        reasons = {"timeout": "TIMEOUT", "overflow": "OUTPUT_LIMIT_EXCEEDED", "io": "IO_UNCONFIRMED", "cleanup": "CLEANUP_UNCONFIRMED", "unreaped": "IO_UNCONFIRMED"}
        outcome.update(reason=reasons[kind], returncode=0 if kind == "cleanup" else None if kind == "unreaped" else -signal.SIGKILL, child_reaped=kind != "unreaped", collection_complete=False, exit_zero_observed=kind == "cleanup", output_complete=False, timed_out=kind == "timeout", forced_termination_requested=kind != "cleanup")
        stdout = stream(b"", eof=kind == "cleanup", prefix=True)
        stderr = stream(b"", eof=kind == "cleanup", prefix=True)
        if kind == "overflow":
            cap = value["max_stream_bytes"]
            stdout = stream(b"O" * cap, eof=False, prefix=True, exceeded=True, observed=cap + 1)
    return {
        "schema": 1, "kind": "FIXTURE_DIAGNOSTIC", "job_id": value["job_id"],
        "target": dict(value["target"]), "assignment_sha256": sha(canonical(value)),
        "fixture_profile": value["fixture_profile"],
        "fixture_code_sha256": sha(collector._PROFILES[value["fixture_profile"]].encode("utf-8")),
        "limits": {key: value[key] for key in ("timeout_ms", "max_stream_bytes")},
        "outcome": outcome, "stdout": stdout, "stderr": stderr,
    }


def connect(path=":memory:", **kwargs):
    connection = sqlite3.connect(path, isolation_level=None, timeout=1, **kwargs)
    connection.execute("PRAGMA synchronous=FULL").close()
    connection.execute("PRAGMA busy_timeout=1000").close()
    return connection


@contextlib.contextmanager
def database(initialized=True):
    with tempfile.TemporaryDirectory(prefix="fixture-ledger-") as directory:
        path = Path(directory) / "ledger.sqlite"
        connection = connect(path)
        try:
            if initialized:
                result = q.initialize_fixture_ledger(connection)
                if result["report"]["reason"] != "LEDGER_READY":
                    raise AssertionError("fixture database initialization failed")
            yield connection, path
        finally:
            connection.close()


def flags(connection):
    return (connection.isolation_level, connection.row_factory, connection.text_factory,
            connection.execute("PRAGMA synchronous").fetchone(),
            connection.execute("PRAGMA journal_mode").fetchone(),
            connection.execute("PRAGMA busy_timeout").fetchone())


RACE_HARNESS = r"""import base64,json,os,sqlite3,sys,time
sys.path.insert(0,sys.argv[1])
from src.ops.tools import github_fixture_ledger as q
payload=json.loads(base64.b64decode(sys.argv[3]))
conn=sqlite3.connect(sys.argv[2],isolation_level=None,timeout=1)
conn.execute('PRAGMA synchronous=FULL').close()
conn.execute('PRAGMA busy_timeout='+str(payload.get('busy_timeout',1000))).close()
os.write(1,b'READY\n')
start=float(sys.stdin.buffer.readline())
time.sleep(max(0,start-time.monotonic()))
if payload['operation']=='assign':
    raw=base64.b64decode(payload['raw'])
    result=q.assign_fixture_job(conn,payload['pin'],raw)
else:
    raw=base64.b64decode(payload['raw'])
    result=q.record_fixture_artifact(conn,payload['job'],payload['pin'],0,raw)
row=result['record']
output={'report':result['report'],'assignment_sha256':None if row is None else row['assignment_sha256'],'artifact_sha256':None if row is None else row['artifact_sha256'],'revision':None if row is None else row['revision']}
os.write(1,json.dumps(output,separators=(',',':')).encode('ascii'))
conn.close()
"""


class FixtureLedgerTests(unittest.TestCase):
    def invoke(self, name, connection, *args, reason=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = getattr(q, name)(connection, *args)
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
        self.assertIs(type(result), dict)
        self.assertEqual(set(result), {"report", "record"})
        report, row = result["report"], result["record"]
        self.assertIs(type(report), dict)
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual(report["scope"], "FIXTURE_LEDGER_ONLY")
        self.assertIn(report["reason"], NORMAL | BLOCKED)
        self.assertEqual(report["status"], "OBSERVED" if report["reason"] in NORMAL else "BLOCKED")
        self.assertEqual(report["operation_confirmed"], report["reason"] in NORMAL)
        for key in REPORT_KEYS - {"scope", "status", "reason", "revision"}:
            self.assertIs(type(report[key]), bool, key)
        self.assertTrue(report["fixture_only"])
        self.assertFalse(report["head_code_executed"])
        for key in TRUST:
            self.assertFalse(report[key], key)
        self.assertTrue(report["revision"] is None or type(report["revision"]) is int and report["revision"] in (0, 1))
        if type(connection) is sqlite3.Connection:
            try:
                self.assertEqual(report["transaction_closed"], not connection.in_transaction)
            except sqlite3.ProgrammingError:
                self.assertFalse(report["transaction_closed"])
        else:
            self.assertFalse(report["transaction_closed"])
        public = canonical(report)
        self.assertNotIn(PRIVATE.encode(), public)
        self.assertNotIn(b"ledger-job:1", public)
        if row is None:
            self.assertFalse(report["assignment_digest_verified"])
            self.assertFalse(report["artifact_digest_verified"])
            self.assertIsNone(report["revision"])
        else:
            self.assertIs(type(row), dict)
            self.assertEqual(set(row), RECORD_KEYS)
            self.assertIs(type(row["job_id"]), str)
            self.assertIs(type(row["revision"]), int)
            self.assertIn(row["revision"], (0, 1))
            self.assertEqual(report["revision"], row["revision"])
            self.assertIs(type(row["assignment_bytes"]), bytes)
            self.assertEqual(row["assignment_sha256"], sha(row["assignment_bytes"]))
            self.assertTrue(report["assignment_digest_verified"])
            value = json.loads(row["assignment_bytes"])
            self.assertEqual(row["job_id"], value["job_id"])
            if row["revision"] == 0:
                self.assertFalse(report["artifact_digest_verified"])
                for key in ("artifact_bytes", "artifact_sha256", "binding"):
                    self.assertIsNone(row[key])
            else:
                self.assertIs(type(row["artifact_bytes"]), bytes)
                self.assertEqual(row["artifact_sha256"], sha(row["artifact_bytes"]))
                self.assertEqual(row["binding"], {**value["target"], "evidence_sha256": sha(row["artifact_bytes"])})
                self.assertTrue(report["artifact_digest_verified"])
        if report["status"] == "BLOCKED":
            self.assertIsNone(row)
        if reason is not None:
            self.assertEqual(report["reason"], reason)
        return result

    def assigned(self, connection, value=None):
        value = assignment() if value is None else value
        raw = canonical(value)
        self.invoke("assign_fixture_job", connection, sha(raw), raw, reason="ASSIGNED")
        return value, raw

    def row(self, connection):
        return connection.execute("SELECT job_id,assignment,assignment_sha256,revision,artifact,artifact_sha256 FROM fixture_jobs ORDER BY job_id").fetchall()

    def run_processes(self, path, payloads, started=None):
        children = []
        try:
            for payload in payloads:
                encoded = base64.b64encode(canonical(payload)).decode("ascii")
                child = subprocess.Popen([sys.executable, "-I", "-B", "-c", RACE_HARNESS, str(ROOT), str(path), encoded], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
                children.append(child)
            for child in children:
                with selectors.DefaultSelector() as selected:
                    selected.register(child.stdout, selectors.EVENT_READ)
                    self.assertTrue(selected.select(5), "fixture harness readiness timeout")
                    self.assertEqual(os.read(child.stdout.fileno(), 64), b"READY\n")
            release = time.monotonic() + 0.05
            if started is not None:
                started.append(release)
            for child in children:
                child.stdin.write((str(release) + "\n").encode("ascii"))
                child.stdin.close()
                child.stdin = None
            results = []
            for child in children:
                stdout, stderr = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0)
                self.assertEqual(stderr, b"")
                result = json.loads(stdout)
                self.assertEqual(set(result), {"report", "assignment_sha256", "artifact_sha256", "revision"})
                self.assertEqual(set(result["report"]), REPORT_KEYS)
                for key in TRUST:
                    self.assertFalse(result["report"][key])
                self.assertTrue(result["report"]["transaction_closed"])
                results.append(result)
            return results
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=2)
                for pipe in (child.stdin, child.stdout, child.stderr):
                    if pipe is not None:
                        pipe.close()

    def test_01_initialize_schema_and_reopen(self):
        for location in ("memory", "file"):
            with self.subTest(location=location), tempfile.TemporaryDirectory(prefix="fixture-ledger-schema-") as directory:
                path = ":memory:" if location == "memory" else str(Path(directory) / "ledger.sqlite")
                connection = connect(path)
                try:
                    before = flags(connection)
                    self.invoke("initialize_fixture_ledger", connection, reason="LEDGER_READY")
                    self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0], 1095779916)
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
                    objects = connection.execute("SELECT type,name,sql FROM sqlite_schema").fetchall()
                    self.assertEqual([(x[0], x[1]) for x in objects], [("table", "fixture_jobs")])
                    self.assertEqual(objects[0][2], EXPECTED_DDL)
                    self.assertIn("WITHOUT ROWID", objects[0][2].upper())
                    columns = connection.execute("PRAGMA table_info(fixture_jobs)").fetchall()
                    self.assertEqual([(x[1], x[2], x[3], x[5]) for x in columns], [("job_id", "TEXT", 1, 1), ("assignment", "BLOB", 1, 0), ("assignment_sha256", "TEXT", 1, 0), ("revision", "INTEGER", 1, 0), ("artifact", "BLOB", 0, 0), ("artifact_sha256", "TEXT", 0, 0)])
                    for revision, content, pin in ((2, None, None), (0, b"x", "a" * 64), (1, None, None)):
                        with self.assertRaises(sqlite3.IntegrityError):
                            connection.execute("INSERT INTO fixture_jobs(job_id,assignment,assignment_sha256,revision,artifact,artifact_sha256) VALUES(?,?,?,?,?,?)", ("invalid", b"x", "a" * 64, revision, content, pin))
                    self.assigned(connection)
                    original = self.row(connection)
                    self.invoke("initialize_fixture_ledger", connection, reason="LEDGER_READY")
                    self.assertEqual(self.row(connection), original)
                    self.assertEqual(flags(connection), before)
                    if location == "file":
                        connection.close()
                        connection = connect(path)
                        self.invoke("read_fixture_job", connection, "ledger-job:1", reason="JOB_OBSERVED")
                finally:
                    connection.close()
        with database(initialized=False) as (connection, _):
            value = canonical(assignment())
            self.invoke("assign_fixture_job", connection, sha(value), value, reason="SCHEMA_INVALID")
            self.invoke("read_fixture_job", connection, "ledger-job:1", reason="SCHEMA_INVALID")
            self.assertEqual(connection.execute("SELECT name FROM sqlite_schema").fetchall(), [])

    def test_02_assignment_raw_replay_and_conflict(self):
        with database() as (connection, path):
            self.invoke("read_fixture_job", connection, "ledger-job:1", reason="JOB_NOT_FOUND")
            value, raw = self.assigned(connection)
            original = self.row(connection)
            self.invoke("assign_fixture_job", connection, sha(raw), raw, reason="ASSIGNMENT_REPLAYED")
            other = raw + b" "
            self.invoke("assign_fixture_job", connection, sha(other), other, reason="ASSIGNMENT_CONFLICT")
            self.assertEqual(self.row(connection), original)
            connection.close()
            reopened = connect(path)
            try:
                result = self.invoke("read_fixture_job", reopened, value["job_id"], reason="JOB_OBSERVED")
                self.assertEqual(result["record"]["assignment_bytes"], raw)
                self.assertEqual(result["record"]["revision"], 0)
            finally:
                reopened.close()

    def test_03_sealed_artifact_replay_and_conflict(self):
        with database() as (connection, path):
            value, raw = self.assigned(connection)
            first = canonical(artifact(value, tag=b"first"))
            second = canonical(artifact(value, tag=b"second"))
            self.invoke("record_fixture_artifact", connection, value["job_id"], sha(raw), 0, first, reason="ARTIFACT_RECORDED")
            original = self.row(connection)
            self.invoke("record_fixture_artifact", connection, value["job_id"], sha(raw), 0, first, reason="ARTIFACT_REPLAYED")
            self.invoke("record_fixture_artifact", connection, value["job_id"], sha(raw), 0, second, reason="ARTIFACT_CONFLICT")
            self.assertEqual(self.row(connection), original)
            connection.close()
            reopened = connect(path)
            try:
                result = self.invoke("read_fixture_job", reopened, value["job_id"], reason="JOB_OBSERVED")
                self.assertEqual(result["record"]["artifact_bytes"], first)
                self.assertEqual(result["record"]["artifact_sha256"], sha(first))
            finally:
                reopened.close()

    def test_04_record_pins_and_revision(self):
        with database() as (connection, _):
            value, raw = self.assigned(connection)
            content = canonical(artifact(value))
            original = self.row(connection)
            wrong_pin = ("0" if sha(raw)[0] != "0" else "1") + sha(raw)[1:]
            self.invoke("record_fixture_artifact", connection, value["job_id"], wrong_pin, 0, content, reason="ASSIGNMENT_PIN_MISMATCH")
            self.assertEqual(self.row(connection), original)
            for revision in (True, False, "0", 0.0, None, -1, 1):
                with self.subTest(revision=revision):
                    trace = []
                    connection.set_trace_callback(trace.append)
                    try:
                        reason = "REVISION_CONFLICT" if type(revision) is int else "ARGUMENTS_INVALID"
                        self.invoke("record_fixture_artifact", connection, value["job_id"], sha(raw), revision, content, reason=reason)
                        self.assertEqual(trace, [])
                    finally:
                        connection.set_trace_callback(None)
            self.assertEqual(self.row(connection), original)
            self.invoke("record_fixture_artifact", connection, "missing-job", sha(raw), 0, content, reason="JOB_NOT_FOUND")

    def test_05_assignment_and_job_input_rejections(self):
        class HookBytes(bytes):
            def __len__(self):
                raise AssertionError(PRIVATE)

        class HookString(str):
            def __str__(self):
                raise AssertionError(PRIVATE)

        with database() as (connection, _):
            raw = canonical(assignment())
            for pin, content in ((sha(raw), None), (sha(raw), bytearray(raw)), (sha(raw), HookBytes(raw)), (sha(raw), b""), (sha(raw), b"x" * 16385), (None, raw), ("A" * 64, raw), (HookString(sha(raw)), raw)):
                with self.subTest(type=type(content).__name__, pin_type=type(pin).__name__):
                    self.invoke("assign_fixture_job", connection, pin, content, reason="ARGUMENTS_INVALID")
            wrong_pin = ("0" if sha(raw)[0] != "0" else "1") + sha(raw)[1:]
            self.invoke("assign_fixture_job", connection, wrong_pin, raw, reason="ASSIGNMENT_HASH_MISMATCH")
            bad = [b"0", b"{}", b"NaN", b"Infinity", b"\xef\xbb\xbf" + raw, b"\xff", b'{"x":123456789012345678901}', b'{"x":' + b"[" * 9 + b"0" + b"]" * 9 + b"}", b'{"schema":1,' + raw[1:]]
            values = [assignment(schema=True), assignment(job_id=""), assignment(job_id="x" * 129), assignment(job_id="bad space"), assignment(mode="unknown"), assignment(fixture_profile="unknown"), assignment(timeout_ms=99), assignment(timeout_ms=2001), assignment(max_stream_bytes=0), assignment(max_stream_bytes=65537), assignment(target={**TARGET, "pr_number": True}), assignment(target={**TARGET, "head_sha": "B" * 40}), assignment(extra=PRIVATE)]
            bad.extend(canonical(value) for value in values)
            for content in bad:
                with self.subTest(digest=sha(content)):
                    self.invoke("assign_fixture_job", connection, sha(content), content, reason="ASSIGNMENT_INVALID")
            content = canonical(assignment(mode="head"))
            self.invoke("assign_fixture_job", connection, sha(content), content, reason="REAL_EXECUTION_BLOCKED")
            for job in (None, True, "", "bad space", "x" * 129, HookString("ledger-job:1")):
                self.invoke("read_fixture_job", connection, job, reason="ARGUMENTS_INVALID")
            self.assertEqual(self.row(connection), [])
            for value in (assignment(timeout_ms=100), assignment(timeout_ms=2000), assignment(max_stream_bytes=1), assignment(max_stream_bytes=65536)):
                value["job_id"] = "limits:" + str(value["timeout_ms"]) + ":" + str(value["max_stream_bytes"])
                content = canonical(value)
                expected = "ASSIGNMENT_REPLAYED" if connection.execute("SELECT 1 FROM fixture_jobs WHERE job_id=?", (value["job_id"],)).fetchone() else "ASSIGNED"
                self.invoke("assign_fixture_job", connection, sha(content), content, reason=expected)
            largest = canonical(assignment(job_id="maxsize-job")).ljust(16384, b" ")
            self.invoke("assign_fixture_job", connection, sha(largest), largest, reason="ASSIGNED")

    def test_06_artifact_shape_canonical_and_identity(self):
        class BlobSubclass(bytes):
            pass

        with database() as (connection, _):
            value, raw = self.assigned(connection)
            good = artifact(value)
            valid = canonical(good)
            bad = [None, bytearray(valid), memoryview(valid), BlobSubclass(valid), b"", b" " * 16385, valid + b" ", json.dumps(good).encode(), b"NaN", b"Infinity", b"\xef\xbb\xbf" + valid, b"\xff", b'{"schema":1,' + valid[1:], b'{"x":123456789012345678901}', b'{"x":' + b"[" * 9 + b"0" + b"]" * 9 + b"}"]
            changes = [{**good, "unknown": PRIVATE}]
            for key in good:
                copy_good = copy.deepcopy(good)
                copy_good.pop(key)
                changes.append(copy_good)
            for key, new in (("schema", True), ("kind", "REQUIRED_TEST_PASS"), ("job_id", "other-job"), ("assignment_sha256", "f" * 64), ("fixture_profile", "nonzero"), ("fixture_code_sha256", "f" * 64)):
                changed = copy.deepcopy(good)
                changed[key] = new
                changes.append(changed)
            for key in TARGET:
                changed = copy.deepcopy(good)
                changed["target"][key] = 2 if key == "pr_number" else "f" * (40 if key.endswith("_sha") else 64) if key.endswith(("_sha", "_sha256")) else "OtherOrg/fixture"
                changes.append(changed)
            for section, key, new in (("limits", "timeout_ms", 999), ("limits", "max_stream_bytes", 65535), ("outcome", "child_reaped", 1), ("stdout", "eof", 1), ("stderr", "captured_bytes", True)):
                changed = copy.deepcopy(good)
                changed[section][key] = new
                changes.append(changed)
            for section in ("target", "limits", "outcome", "stdout", "stderr"):
                changed = copy.deepcopy(good)
                changed[section]["unknown"] = PRIVATE
                changes.append(changed)
            bad.extend(canonical(value) for value in changes)
            original = self.row(connection)
            for content in bad:
                with self.subTest(content_type=type(content).__name__, digest=sha(content) if type(content) is bytes else None):
                    self.invoke("record_fixture_artifact", connection, value["job_id"], sha(raw), 0, content, reason="ARTIFACT_INVALID")
                    self.assertEqual(self.row(connection), original)

    def test_07_artifact_outcomes_and_internal_consistency(self):
        for kind in ("complete", "nonzero", "signal", "timeout", "overflow", "io", "cleanup", "unreaped"):
            value = assignment(max_stream_bytes=64)
            content = canonical(artifact(value, kind=kind))
            with self.subTest(valid_kind=kind), database() as (connection, _):
                _, raw = self.assigned(connection, value)
                self.invoke("record_fixture_artifact", connection, value["job_id"], sha(raw), 0, content, reason="ARTIFACT_RECORDED")
        with database() as (connection, _):
            value, raw = self.assigned(connection)
            good = artifact(value)
            changes = []
            for section, key, new in (("outcome", "returncode", 7), ("outcome", "returncode", None), ("outcome", "reason", "SIGNALED"), ("outcome", "child_reaped", False), ("outcome", "timed_out", True), ("outcome", "output_complete", False), ("outcome", "forced_termination_requested", True), ("stdout", "prefix_only", True), ("stdout", "eof", False), ("stdout", "captured_bytes", 65537), ("stdout", "observed_bytes", 65538), ("stdout", "observed_bytes", 0), ("stdout", "limit_exceeded", True)):
                changed = copy.deepcopy(good)
                changed[section][key] = new
                changes.append(changed)
            changed = artifact(value, kind="timeout")
            changed["stdout"]["sha256"] = "f" * 64
            changes.append(changed)
            changed = artifact(value, kind="timeout")
            changed["stderr"]["prefix_only"] = False
            changes.append(changed)
            original = self.row(connection)
            for changed in changes:
                with self.subTest(changed=sha(canonical(changed))):
                    self.invoke("record_fixture_artifact", connection, value["job_id"], sha(raw), 0, canonical(changed), reason="ARTIFACT_INVALID")
                    self.assertEqual(self.row(connection), original)

    def test_08_stored_row_corruption_and_blob_cap(self):
        cases = [("assignment", b"{}"), ("assignment_sha256", "f" * 64), ("artifact_sha256", "f" * 64), ("artifact", b"{}"), ("assignment", b"x" * (1024 * 1024)), ("artifact", b"x" * (1024 * 1024)), ("assignment", "not-blob"), ("artifact", "not-blob"), ("revision", 2)]
        for column, corrupt in cases:
            with self.subTest(column=column, size=len(corrupt) if isinstance(corrupt, (str, bytes)) else None), database() as (connection, _):
                value, raw = self.assigned(connection)
                content = canonical(artifact(value))
                self.invoke("record_fixture_artifact", connection, value["job_id"], sha(raw), 0, content, reason="ARTIFACT_RECORDED")
                connection.execute("PRAGMA ignore_check_constraints=ON").close()
                connection.execute("UPDATE fixture_jobs SET " + column + "=? WHERE job_id=?", (corrupt, value["job_id"])).close()
                connection.execute("PRAGMA ignore_check_constraints=OFF").close()
                self.invoke("read_fixture_job", connection, value["job_id"], reason="ROW_INVALID")
                stored = connection.execute("SELECT " + column + " FROM fixture_jobs WHERE job_id=?", (value["job_id"],)).fetchone()[0]
                self.assertEqual(stored, corrupt)
        with database() as (connection, _):
            value, _ = self.assigned(connection)
            real_query = q._query

            def database_error(conn, sql, parameters=()):
                if sql == q._ROW_SQL:
                    raise sqlite3.DatabaseError(PRIVATE)
                return real_query(conn, sql, parameters)

            with mock.patch.object(q, "_query", side_effect=database_error):
                self.invoke("read_fixture_job", connection, value["job_id"], reason="DATABASE_UNCONFIRMED")

    def test_09_schema_and_namespace_denials(self):
        schema_cases = ["PRAGMA application_id=1", "PRAGMA user_version=2", "CREATE TABLE extra(x)", "CREATE VIEW extra AS SELECT 1 AS x", "CREATE INDEX extra ON fixture_jobs(revision)", "CREATE TRIGGER extra AFTER INSERT ON fixture_jobs BEGIN SELECT 1; END", "CREATE TABLE \"" + "x" * 129 + "\"(x)"]
        for sql in schema_cases:
            with self.subTest(sql_prefix=sql[:35]), database() as (connection, _):
                connection.execute(sql).close()
                before = connection.execute("SELECT type,name,sql FROM sqlite_schema ORDER BY name").fetchall()
                self.invoke("read_fixture_job", connection, "ledger-job:1", reason="SCHEMA_INVALID")
                self.invoke("initialize_fixture_ledger", connection, reason="SCHEMA_INVALID")
                self.assertEqual(connection.execute("SELECT type,name,sql FROM sqlite_schema ORDER BY name").fetchall(), before)
        with database() as (connection, _):
            connection.execute("DROP TABLE fixture_jobs").close()
            connection.execute("CREATE TABLE fixture_jobs(x TEXT DEFAULT '" + "a" * 4097 + "')").close()
            self.invoke("read_fixture_job", connection, "ledger-job:1", reason="SCHEMA_INVALID")
        for sql in ("ATTACH DATABASE ':memory:' AS extra", "CREATE TEMP TABLE extra(x)"):
            with self.subTest(namespace=sql), database() as (connection, _):
                connection.execute(sql).close()
                self.invoke("read_fixture_job", connection, "ledger-job:1", reason="CONNECTION_INVALID")

    def test_10_connection_flags_transaction_and_readonly(self):
        class SubConnection(sqlite3.Connection):
            pass

        self.invoke("initialize_fixture_ledger", None, reason="CONNECTION_INVALID")
        derived = connect(factory=SubConnection)
        try:
            self.invoke("initialize_fixture_ledger", derived, reason="CONNECTION_INVALID")
        finally:
            derived.close()
        closed = connect()
        closed.close()
        self.invoke("initialize_fixture_ledger", closed, reason="CONNECTION_INVALID")
        for kind in ("isolation", "row", "text", "sync", "journal", "busy"):
            with self.subTest(kind=kind), database() as (connection, _):
                if kind == "isolation":
                    connection.isolation_level = "DEFERRED"
                elif kind == "row":
                    connection.row_factory = sqlite3.Row
                elif kind == "text":
                    connection.text_factory = bytes
                elif kind == "sync":
                    connection.execute("PRAGMA synchronous=OFF").close()
                elif kind == "journal":
                    connection.execute("PRAGMA journal_mode=WAL").close()
                else:
                    connection.execute("PRAGMA busy_timeout=1001").close()
                before = flags(connection)
                self.invoke("read_fixture_job", connection, "ledger-job:1", reason="CONNECTION_INVALID")
                self.assertEqual(flags(connection), before)
        with database() as (connection, path):
            self.assigned(connection)
            connection.execute("BEGIN IMMEDIATE").close()
            trace = []
            connection.set_trace_callback(trace.append)
            try:
                result = self.invoke("read_fixture_job", connection, "ledger-job:1", reason="CONNECTION_INVALID")
                self.assertFalse(result["report"]["transaction_closed"])
                self.assertEqual(trace, [])
                self.assertTrue(connection.in_transaction)
            finally:
                connection.set_trace_callback(None)
                connection.rollback()
            readonly = connect("file:" + str(path) + "?mode=ro", uri=True)
            try:
                original_rows = self.row(readonly)
                original_bytes = path.read_bytes()
                original_flags = flags(readonly)
                self.invoke("read_fixture_job", readonly, "ledger-job:1", reason="JOB_OBSERVED")
                initialized = self.invoke("initialize_fixture_ledger", readonly)
                self.assertIn(initialized["report"]["reason"], {"LEDGER_READY", "DATABASE_UNCONFIRMED"})
                self.assertEqual(self.row(readonly), original_rows)
                self.assertEqual(path.read_bytes(), original_bytes)
                self.assertEqual(flags(readonly), original_flags)
                value = canonical(assignment(job_id="other-job"))
                written = self.invoke("assign_fixture_job", readonly, sha(value), value, reason="DATABASE_UNCONFIRMED")
                self.assertEqual(self.row(readonly), original_rows)
                self.assertEqual(path.read_bytes(), original_bytes)
                self.assertEqual(flags(readonly), original_flags)
                OBSERVATIONS.append({"case": "R10", "existing_readonly_initialize": initialized["report"]["reason"], "new_job_write": written["report"]["reason"], "bytes_rows_flags_preserved": True})
            finally:
                readonly.close()
        with tempfile.TemporaryDirectory(prefix="fixture-ledger-readonly-empty-") as directory:
            path = Path(directory) / "empty.sqlite"
            empty = connect(path)
            empty.close()
            readonly = connect("file:" + str(path) + "?mode=ro", uri=True)
            try:
                original_bytes = path.read_bytes()
                original_flags = flags(readonly)
                initialized = self.invoke("initialize_fixture_ledger", readonly, reason="DATABASE_UNCONFIRMED")
                self.assertEqual(readonly.execute("SELECT name FROM sqlite_schema").fetchall(), [])
                self.assertEqual(readonly.execute("PRAGMA application_id").fetchone()[0], 0)
                self.assertEqual(readonly.execute("PRAGMA user_version").fetchone()[0], 0)
                self.assertEqual(path.read_bytes(), original_bytes)
                self.assertEqual(flags(readonly), original_flags)
                OBSERVATIONS.append({"case": "R10", "empty_readonly_initialize": initialized["report"]["reason"], "schema_application_version_unchanged": True, "bytes_flags_preserved": True})
            finally:
                readonly.close()

    def test_11_real_assignment_process_races(self):
        first = canonical(assignment())
        for same in (True, False):
            second = first if same else first + b" "
            with self.subTest(same=same), database() as (connection, path):
                payloads = [{"operation": "assign", "raw": base64.b64encode(raw).decode("ascii"), "pin": sha(raw)} for raw in (first, second)]
                started = time.monotonic()
                results = self.run_processes(path, payloads)
                elapsed = time.monotonic() - started
                reasons = sorted(result["report"]["reason"] for result in results)
                self.assertEqual(reasons, sorted(["ASSIGNED", "ASSIGNMENT_REPLAYED" if same else "ASSIGNMENT_CONFLICT"]))
                rows = self.row(connection)
                self.assertEqual(len(rows), 1)
                winner = next(result for result in results if result["report"]["reason"] == "ASSIGNED")
                self.assertEqual(rows[0][2], winner["assignment_sha256"])
                self.assertIn(rows[0][1], (first, second))
                self.assertEqual(rows[0][3:], (0, None, None))
                OBSERVATIONS.append({"case": "R11", "same_bytes": same, "elapsed_seconds": elapsed, "reasons": reasons, "rows": 1})

    def test_12_real_artifact_process_races(self):
        for same in (True, False):
            with self.subTest(same=same), database() as (connection, path):
                value, raw = self.assigned(connection)
                first = canonical(artifact(value, tag=b"first"))
                second = first if same else canonical(artifact(value, tag=b"second"))
                payloads = [{"operation": "record", "job": value["job_id"], "pin": sha(raw), "raw": base64.b64encode(content).decode("ascii")} for content in (first, second)]
                started = time.monotonic()
                results = self.run_processes(path, payloads)
                elapsed = time.monotonic() - started
                reasons = sorted(result["report"]["reason"] for result in results)
                self.assertEqual(reasons, sorted(["ARTIFACT_RECORDED", "ARTIFACT_REPLAYED" if same else "ARTIFACT_CONFLICT"]))
                rows = self.row(connection)
                self.assertEqual(len(rows), 1)
                winner = next(result for result in results if result["report"]["reason"] == "ARTIFACT_RECORDED")
                self.assertEqual(rows[0][3], 1)
                self.assertEqual(rows[0][5], winner["artifact_sha256"])
                self.assertIn(rows[0][4], (first, second))
                observed = self.invoke("read_fixture_job", connection, value["job_id"], reason="JOB_OBSERVED")
                self.assertEqual(observed["record"]["binding"]["evidence_sha256"], sha(rows[0][4]))
                OBSERVATIONS.append({"case": "R12", "same_bytes": same, "elapsed_seconds": elapsed, "reasons": reasons, "revision": 1})

    def test_13_real_busy_lock_is_bounded(self):
        with database() as (connection, path):
            raw = canonical(assignment())
            connection.execute("BEGIN IMMEDIATE").close()
            try:
                payload = {"operation": "assign", "raw": base64.b64encode(raw).decode("ascii"), "pin": sha(raw), "busy_timeout": 100}
                started = time.monotonic()
                results = self.run_processes(path, [payload])
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 2)
                self.assertGreaterEqual(elapsed, 0.1)
                self.assertEqual(results[0]["report"]["reason"], "DATABASE_UNCONFIRMED")
                self.assertEqual(results[0]["report"]["status"], "BLOCKED")
                self.assertTrue(connection.in_transaction)
                self.assertEqual(self.row(connection), [])
                OBSERVATIONS.append({"case": "R13", "busy_timeout_ms": 100, "elapsed_seconds": elapsed, "reason": results[0]["report"]["reason"]})
            finally:
                connection.rollback()
            self.invoke("assign_fixture_job", connection, sha(raw), raw, reason="ASSIGNED")

    def test_14_commit_loss_rollback_and_recovery(self):
        for begin_executed in (False, True):
            with self.subTest(begin_executed=begin_executed), database() as (connection, _):
                value, raw = self.assigned(connection)
                original = self.row(connection)
                real_execute, real_rollback = q._execute, q._rollback

                def begin_response_error(conn, sql, parameters=()):
                    if sql == "BEGIN IMMEDIATE":
                        if begin_executed:
                            real_execute(conn, sql, parameters)
                        raise sqlite3.OperationalError(PRIVATE)
                    return real_execute(conn, sql, parameters)

                with mock.patch.object(q, "_execute", side_effect=begin_response_error), mock.patch.object(q, "_rollback", wraps=real_rollback) as rollback:
                    result = self.invoke("record_fixture_artifact", connection, value["job_id"], sha(raw), 0, canonical(artifact(value)), reason="DATABASE_UNCONFIRMED")
                self.assertTrue(result["report"]["transaction_closed"])
                self.assertEqual(rollback.call_count, int(begin_executed))
                self.assertEqual(self.row(connection), original)
        for after_commit in (False, True):
            with self.subTest(after_commit=after_commit), database() as (connection, path):
                value, raw = self.assigned(connection)
                content = canonical(artifact(value))
                original = self.row(connection)
                real_commit = q._commit

                def lost_response(conn):
                    if after_commit:
                        real_commit(conn)
                    raise sqlite3.OperationalError(PRIVATE)

                with mock.patch.object(q, "_commit", side_effect=lost_response):
                    result = self.invoke("record_fixture_artifact", connection, value["job_id"], sha(raw), 0, content, reason="DATABASE_UNCONFIRMED")
                self.assertTrue(result["report"]["transaction_closed"])
                reopened = connect(path)
                try:
                    row = self.invoke("read_fixture_job", reopened, value["job_id"], reason="JOB_OBSERVED")["record"]
                    self.assertEqual(row["revision"], 1 if after_commit else 0)
                    if after_commit:
                        self.assertEqual(row["artifact_bytes"], content)
                        self.invoke("record_fixture_artifact", reopened, value["job_id"], sha(raw), 0, content, reason="ARTIFACT_REPLAYED")
                    else:
                        self.assertEqual(self.row(reopened), original)
                finally:
                    reopened.close()
        with database() as (connection, path):
            value, raw = self.assigned(connection)
            with mock.patch.object(q, "_commit", side_effect=sqlite3.OperationalError(PRIVATE)), mock.patch.object(q, "_rollback", side_effect=sqlite3.OperationalError(PRIVATE)):
                result = self.invoke("record_fixture_artifact", connection, value["job_id"], sha(raw), 0, canonical(artifact(value)), reason="DATABASE_UNCONFIRMED")
            self.assertFalse(result["report"]["transaction_closed"])
            self.assertTrue(connection.in_transaction)
            connection.rollback()
            self.assertIsNone(self.invoke("read_fixture_job", connection, value["job_id"], reason="JOB_OBSERVED")["record"]["artifact_bytes"])

    def test_15_real_collector_ok_and_nonzero_to_reopened_ledger(self):
        for profile, exit_code in (("ok", 0), ("nonzero", 7)):
            with self.subTest(profile=profile), database() as (connection, path), tempfile.TemporaryDirectory(prefix="fixture-ledger-state-") as directory:
                os.chmod(directory, 0o700)
                value = assignment(profile)
                raw = canonical(value)
                assignment_path = Path(directory) / "assignment.json"
                assignment_path.write_bytes(raw)
                os.chmod(assignment_path, 0o600)
                self.invoke("assign_fixture_job", connection, sha(raw), raw, reason="ASSIGNED")
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                try:
                    collected = collector.collect_fixture_assignment(fd, sha(raw))
                finally:
                    os.close(fd)
                self.assertEqual(collected["report"]["reason"], "EXIT_ZERO" if exit_code == 0 else "EXIT_NONZERO")
                self.assertEqual(collected["report"]["returncode"], exit_code)
                self.assertTrue(collected["report"]["collection_complete"])
                result = self.invoke("record_fixture_artifact", connection, value["job_id"], sha(raw), 0, collected["artifact"], reason="ARTIFACT_RECORDED")
                self.assertEqual(result["record"]["artifact_bytes"], collected["artifact"])
                self.assertEqual(result["record"]["binding"], collected["binding"])
                connection.close()
                reopened = connect(path)
                try:
                    observed = self.invoke("read_fixture_job", reopened, value["job_id"], reason="JOB_OBSERVED")
                    self.assertEqual(observed["record"]["artifact_bytes"], collected["artifact"])
                    self.assertEqual(observed["record"]["artifact_sha256"], sha(collected["artifact"]))
                    self.assertEqual(observed["record"]["binding"], collected["binding"])
                finally:
                    reopened.close()
                OBSERVATIONS.append({"case": "R15", "profile": profile, "returncode": exit_code, "artifact_bytes": len(collected["artifact"]), "collector_calls": 1, "reopen_matches": True})


if __name__ == "__main__":
    unittest.main()
