"""Persist fixed fixture assignments/artifacts; never authorize head execution.

Python 3.10+, borrowed controller-supplied SQLite connection. No path, CLI,
connection creation, signing, network or process execution API. Database
origin, installation, real invocation and rollback resistance stay unverified.
"""

import hashlib
import json
import sqlite3

from src.ops.tools import github_fixture_collector as collector


_APPLICATION_ID = 1095779916
_VERSION = 1
_MAX_BYTES = 16384
_DDL = (
    "CREATE TABLE fixture_jobs ("
    "job_id TEXT NOT NULL PRIMARY KEY, assignment BLOB NOT NULL, "
    "assignment_sha256 TEXT NOT NULL, "
    "revision INTEGER NOT NULL CHECK (revision IN (0,1)), "
    "artifact BLOB, artifact_sha256 TEXT, "
    "CHECK ((revision=0 AND artifact IS NULL AND artifact_sha256 IS NULL) OR "
    "(revision=1 AND artifact IS NOT NULL AND artifact_sha256 IS NOT NULL))"
    ") WITHOUT ROWID"
)
_SCHEMA_SQL = (
    "SELECT CASE WHEN length(type)<=128 THEN CAST(type AS TEXT) END, "
    "CASE WHEN length(name)<=128 THEN CAST(name AS TEXT) END, "
    "CASE WHEN length(tbl_name)<=128 THEN CAST(tbl_name AS TEXT) END, "
    "CASE WHEN length(CAST(sql AS BLOB))<=4096 THEN CAST(sql AS TEXT) END "
    "FROM main.sqlite_master LIMIT 2"
)
_ROW_SQL = (
    "SELECT CASE WHEN typeof(job_id)='text' AND length(job_id)<=128 "
    "THEN CAST(job_id AS TEXT) END, "
    "CASE WHEN typeof(assignment)='blob' AND length(assignment) BETWEEN 1 AND 16384 "
    "THEN CAST(assignment AS BLOB) END, "
    "CASE WHEN typeof(assignment_sha256)='text' AND length(assignment_sha256)=64 "
    "THEN CAST(assignment_sha256 AS TEXT) END, "
    "CASE WHEN typeof(revision)='integer' AND revision IN (0,1) THEN revision END, "
    "CASE WHEN typeof(artifact)='blob' AND length(artifact) BETWEEN 1 AND 16384 "
    "THEN CAST(artifact AS BLOB) END, "
    "CASE WHEN typeof(artifact_sha256)='text' AND length(artifact_sha256)=64 "
    "THEN CAST(artifact_sha256 AS TEXT) END, "
    "artifact IS NULL, artifact_sha256 IS NULL "
    "FROM main.fixture_jobs WHERE job_id=?"
)
_NORMAL = frozenset({"LEDGER_READY", "ASSIGNED", "ASSIGNMENT_REPLAYED",
                     "ARTIFACT_RECORDED", "ARTIFACT_REPLAYED", "JOB_OBSERVED"})
_REASONS = _NORMAL | frozenset({
    "ARGUMENTS_INVALID", "CONNECTION_INVALID", "SCHEMA_INVALID", "ASSIGNMENT_INVALID",
    "ASSIGNMENT_HASH_MISMATCH", "REAL_EXECUTION_BLOCKED", "JOB_NOT_FOUND",
    "ASSIGNMENT_CONFLICT", "ASSIGNMENT_PIN_MISMATCH", "REVISION_CONFLICT",
    "ARTIFACT_INVALID", "ARTIFACT_CONFLICT", "ROW_INVALID", "DATABASE_UNCONFIRMED",
})
_OUTCOME_REASONS = frozenset({"EXIT_ZERO", "EXIT_NONZERO", "SIGNALED", "TIMEOUT",
                              "OUTPUT_LIMIT_EXCEEDED", "IO_UNCONFIRMED", "CLEANUP_UNCONFIRMED"})


class _Failure(Exception):
    def __init__(self, reason):
        self.reason = reason


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _hex(value):
    return (type(value) is str and len(value) == 64
            and collector._SHA256.fullmatch(value) is not None)


def _job(value):
    return (type(value) is str and 1 <= len(value) <= 128
            and collector._JOB.fullmatch(value) is not None)


def _blob(value):
    return type(value) is bytes and 1 <= len(value) <= _MAX_BYTES


def _query(connection, sql, parameters=()):
    cursor = connection.execute(sql, parameters)
    try:
        return cursor.fetchmany(3)
    finally:
        cursor.close()


def _execute(connection, sql, parameters=()):
    cursor = connection.execute(sql, parameters)
    try:
        return cursor.rowcount
    finally:
        cursor.close()


def _one(connection, sql):
    rows = _query(connection, sql)
    if len(rows) != 1 or type(rows[0]) is not tuple or len(rows[0]) != 1:
        raise _Failure("DATABASE_UNCONFIRMED")
    return rows[0][0]


def _commit(connection):
    _execute(connection, "COMMIT")


def _rollback(connection):
    _execute(connection, "ROLLBACK")


def _closed(connection):
    try:
        return type(connection) is sqlite3.Connection and not connection.in_transaction
    except Exception:
        return False


def _result(connection, reason, record=None):
    if type(reason) is not str or reason not in _REASONS:
        reason = "DATABASE_UNCONFIRMED"
    confirmed = reason in _NORMAL
    if not confirmed:
        record = None
    return {"report": {
        "scope": "FIXTURE_LEDGER_ONLY", "status": "OBSERVED" if confirmed else "BLOCKED",
        "reason": reason, "operation_confirmed": confirmed,
        "assignment_digest_verified": record is not None,
        "artifact_digest_verified": record is not None and record["revision"] == 1,
        "revision": record["revision"] if record is not None else None,
        "transaction_closed": _closed(connection), "fixture_only": True,
        "head_code_executed": False, "input_origin_verified": False,
        "signer_roster_origin_verified": False, "issuer_environment_verified": False,
        "runtime_isolation_verified": False, "independent_review_verified": False,
        "trusted_gate_verified": False, "binding_verified": False,
        "native_ready": False, "merge_authorized": False,
    }, "record": record}


def _connection(connection):
    try:
        if (type(connection) is not sqlite3.Connection
                or connection.isolation_level is not None or connection.row_factory is not None
                or connection.text_factory is not str or connection.in_transaction):
            raise ValueError()
        busy = _one(connection, "PRAGMA busy_timeout")
        if type(busy) is not int or not 0 <= busy <= 1000:
            raise ValueError()
    except Exception:
        raise _Failure("CONNECTION_INVALID") from None


def _schema(connection, initialize):
    # This runs inside our transaction; another writer cannot replace the
    # schema between its validation and our statements.
    objects = _query(connection, _SCHEMA_SQL)
    databases = _query(connection, "PRAGMA database_list")
    temporary = _query(connection, "SELECT 1 FROM temp.sqlite_master LIMIT 1")
    if (not databases or any(row[1] not in ("main", "temp") for row in databases)
            or sum(row[1] == "main" for row in databases) != 1 or temporary):
        raise _Failure("CONNECTION_INVALID")
    if (_one(connection, "PRAGMA main.journal_mode") not in ("delete", "memory")
            or _one(connection, "PRAGMA main.synchronous") not in (2, 3)):
        raise _Failure("CONNECTION_INVALID")
    app = _one(connection, "PRAGMA main.application_id")
    version = _one(connection, "PRAGMA main.user_version")
    if initialize and not objects and app == 0 and version == 0:
        _execute(connection, _DDL)
        _execute(connection, "PRAGMA main.application_id=1095779916")
        _execute(connection, "PRAGMA main.user_version=1")
        objects = _query(connection, _SCHEMA_SQL)
        app = _one(connection, "PRAGMA main.application_id")
        version = _one(connection, "PRAGMA main.user_version")
    if (type(app) is not int or app != _APPLICATION_ID
            or type(version) is not int or version != _VERSION
            or objects != [("table", "fixture_jobs", "fixture_jobs", _DDL)]):
        raise _Failure("SCHEMA_INVALID")


def _assignment(raw, expected):
    if not _blob(raw) or not _hex(expected):
        raise _Failure("ARGUMENTS_INVALID")
    if _sha(raw) != expected:
        raise _Failure("ASSIGNMENT_HASH_MISMATCH")
    try:
        return collector._assignment(raw)
    except collector._Failure as error:
        raise _Failure(error.reason) from None


def _artifact(raw, assignment, assignment_sha256):
    try:
        if not _blob(raw) or raw.startswith(b"\xef\xbb\xbf"):
            raise ValueError()
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=collector._unique,
                          parse_int=collector._integer, parse_constant=collector._nonfinite)
        stack = [(data, 1)]
        while stack:
            value, depth = stack.pop()
            if type(value) in (dict, list):
                if depth > 8:
                    raise ValueError()
                children = value.values() if type(value) is dict else value
                stack.extend((item, depth + 1) for item in children)
        if (collector._canonical(data) != raw
                or not collector._shape(data, ("schema", "kind", "job_id", "target",
                                               "assignment_sha256", "fixture_profile",
                                               "fixture_code_sha256", "limits", "outcome",
                                               "stdout", "stderr"))
                or type(data["schema"]) is not int or data["schema"] != 1
                or data["kind"] != "FIXTURE_DIAGNOSTIC"):
            raise ValueError()
        expected = {"job_id": assignment["job_id"], "target": assignment["target"],
                    "assignment_sha256": assignment_sha256,
                    "fixture_profile": assignment["fixture_profile"],
                    "fixture_code_sha256": _sha(collector._PROFILES[assignment["fixture_profile"]].encode("utf-8")),
                    "limits": {key: assignment[key] for key in ("timeout_ms", "max_stream_bytes")}}
        if any(collector._canonical(data[key]) != collector._canonical(value)
               for key, value in expected.items()):
            raise ValueError()
        outcome = data["outcome"]
        flags = ("child_reaped", "collection_complete", "exit_zero_observed", "output_complete",
                 "timed_out", "forced_termination_requested")
        if (not collector._shape(outcome, ("reason", "returncode", *flags))
                or type(outcome["reason"]) is not str or outcome["reason"] not in _OUTCOME_REASONS
                or any(type(outcome[key]) is not bool for key in flags)):
            raise ValueError()
        rc, reaped, complete = outcome["returncode"], outcome["child_reaped"], outcome["output_complete"]
        if ((rc is not None and type(rc) is not int) or (rc is None) != (not reaped)
                or outcome["exit_zero_observed"] != (reaped and rc == 0)
                or outcome["collection_complete"] != complete
                or outcome["timed_out"] != (outcome["reason"] == "TIMEOUT")):
            raise ValueError()
        cap, overflow, eof = assignment["max_stream_bytes"], False, True
        for name in ("stdout", "stderr"):
            row = data[name]
            if (not collector._shape(row, ("captured_bytes", "observed_bytes", "sha256",
                                           "prefix_only", "eof", "limit_exceeded"))
                    or type(row["captured_bytes"]) is not int or not 0 <= row["captured_bytes"] <= cap
                    or type(row["observed_bytes"]) is not int or not 0 <= row["observed_bytes"] <= cap + 1
                    or not _hex(row["sha256"])
                    or any(type(row[key]) is not bool for key in ("prefix_only", "eof", "limit_exceeded"))
                    or row["prefix_only"] != (not complete)
                    or row["observed_bytes"] != row["captured_bytes"] + int(row["limit_exceeded"])
                    or (row["captured_bytes"] == 0 and row["sha256"] != _sha(b""))
                    or (row["limit_exceeded"] and (row["captured_bytes"] != cap or row["eof"]))):
                raise ValueError()
            overflow = overflow or row["limit_exceeded"]
            eof = eof and row["eof"]
        reason = outcome["reason"]
        success_reason = reason in ("EXIT_ZERO", "EXIT_NONZERO", "SIGNALED")
        if (overflow != (reason == "OUTPUT_LIMIT_EXCEEDED") or success_reason != complete
                or (complete and (not reaped or not eof or overflow or outcome["timed_out"]
                                  or outcome["forced_termination_requested"]))
                or (reason == "EXIT_ZERO" and rc != 0)
                or (reason == "EXIT_NONZERO" and (rc is None or rc <= 0))
                or (reason == "SIGNALED" and (rc is None or rc >= 0))):
            raise ValueError()
    except Exception:
        raise _Failure("ARTIFACT_INVALID") from None


def _read(connection, job_id):
    rows = _query(connection, _ROW_SQL, (job_id,))
    if not rows:
        return None
    try:
        if len(rows) != 1 or type(rows[0]) is not tuple or len(rows[0]) != 8:
            raise ValueError()
        job, raw, assignment_sha, revision, artifact, artifact_sha, artifact_null, sha_null = rows[0]
        if (not _job(job) or job != job_id or type(revision) is not int or revision not in (0, 1)):
            raise ValueError()
        assignment = _assignment(raw, assignment_sha)
        if assignment["job_id"] != job:
            raise ValueError()
        if revision == 0:
            if artifact_null != 1 or sha_null != 1 or artifact is not None or artifact_sha is not None:
                raise ValueError()
            binding = None
        else:
            if not _blob(artifact) or not _hex(artifact_sha) or _sha(artifact) != artifact_sha:
                raise ValueError()
            _artifact(artifact, assignment, assignment_sha)
            binding = dict(assignment["target"], evidence_sha256=artifact_sha)
        return {"job_id": job, "revision": revision, "assignment_bytes": raw,
                "assignment_sha256": assignment_sha, "artifact_bytes": artifact,
                "artifact_sha256": artifact_sha, "binding": binding}
    except Exception:
        raise _Failure("ROW_INVALID") from None


def _run(connection, action, write=True, initialize=False):
    owned, record = False, None
    try:
        _connection(connection)
        # Entry has no caller transaction. BEGIN can succeed before a cursor
        # close/response error, so own that attempt and inspect actual state.
        owned = True
        _execute(connection, "BEGIN IMMEDIATE" if write else "BEGIN")
        _schema(connection, initialize)
        reason, record = action()
        if reason != "LEDGER_READY" and record is None:
            raise _Failure("DATABASE_UNCONFIRMED")
        _commit(connection)
        if connection.in_transaction:
            raise _Failure("DATABASE_UNCONFIRMED")
        owned = False
    except _Failure as error:
        reason, record = error.reason, None
    except Exception:
        reason, record = "DATABASE_UNCONFIRMED", None
    finally:
        if owned:
            try:
                if connection.in_transaction:
                    _rollback(connection)
            except Exception:
                reason, record = "DATABASE_UNCONFIRMED", None
    return _result(connection, reason, record)


def initialize_fixture_ledger(connection):
    """Initialize only an empty dedicated DB, or confirm the exact schema."""
    return _run(connection, lambda: ("LEDGER_READY", None), initialize=True)


def assign_fixture_job(connection, expected_assignment_sha256, assignment_bytes):
    """Keep a job's original assignment bytes; same bytes are idempotent."""
    try:
        assignment = _assignment(assignment_bytes, expected_assignment_sha256)
    except _Failure as error:
        return _result(connection, error.reason)
    except Exception:
        return _result(connection, "DATABASE_UNCONFIRMED")

    def action():
        row = _read(connection, assignment["job_id"])
        if row is not None:
            if row["assignment_bytes"] != assignment_bytes:
                raise _Failure("ASSIGNMENT_CONFLICT")
            return "ASSIGNMENT_REPLAYED", row
        _execute(connection, "INSERT INTO main.fixture_jobs VALUES (?,?,?,0,NULL,NULL)",
                 (assignment["job_id"], assignment_bytes, expected_assignment_sha256))
        return "ASSIGNED", _read(connection, assignment["job_id"])

    return _run(connection, action)


def read_fixture_job(connection, job_id):
    """Recheck one stored row and return its exact bytes, without execution."""
    if not _job(job_id):
        return _result(connection, "ARGUMENTS_INVALID")

    def action():
        row = _read(connection, job_id)
        if row is None:
            raise _Failure("JOB_NOT_FOUND")
        return "JOB_OBSERVED", row

    return _run(connection, action, write=False)


def record_fixture_artifact(connection, job_id, expected_assignment_sha256,
                            expected_revision, artifact_bytes):
    """Seal once with revision0; exact replay returns the existing artifact."""
    if (not _job(job_id) or not _hex(expected_assignment_sha256)
            or type(expected_revision) is not int):
        return _result(connection, "ARGUMENTS_INVALID")
    if expected_revision != 0:
        return _result(connection, "REVISION_CONFLICT")
    if not _blob(artifact_bytes):
        return _result(connection, "ARTIFACT_INVALID")

    def action():
        row = _read(connection, job_id)
        if row is None:
            raise _Failure("JOB_NOT_FOUND")
        if row["assignment_sha256"] != expected_assignment_sha256:
            raise _Failure("ASSIGNMENT_PIN_MISMATCH")
        assignment = collector._assignment(row["assignment_bytes"])
        _artifact(artifact_bytes, assignment, row["assignment_sha256"])
        if row["revision"] == 1:
            if row["artifact_bytes"] != artifact_bytes:
                raise _Failure("ARTIFACT_CONFLICT")
            return "ARTIFACT_REPLAYED", row
        changed = _execute(connection,
                           "UPDATE main.fixture_jobs SET revision=1,artifact=?,artifact_sha256=? "
                           "WHERE job_id=? AND assignment_sha256=? AND revision=? "
                           "AND artifact IS NULL AND artifact_sha256 IS NULL",
                           (artifact_bytes, _sha(artifact_bytes), job_id, row["assignment_sha256"], 0))
        if changed != 1:
            raise _Failure("REVISION_CONFLICT")
        return "ARTIFACT_RECORDED", _read(connection, job_id)

    return _run(connection, action)
