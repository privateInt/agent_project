"""Connect pinned fixture collection and storage, without authorizing head work.

Two borrowed, dedicated SQLite connections; no paths, connection creation,
automatic retries, signing or CLI. Durable claims intentionally do not expire.
"""

import hashlib
import json
import sqlite3

from src.ops.tools import github_fixture_collector as collector
from src.ops.tools import github_fixture_ledger as ledger


_APPLICATION_ID = 1095779907
_DDL = (
    "CREATE TABLE fixture_controller_jobs (job_id TEXT NOT NULL PRIMARY KEY, "
    "assignment_sha256 TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN "
    "('CLAIMED','PREPARATION_FAILED')), preparation_reason TEXT, CHECK "
    "((state='CLAIMED' AND preparation_reason IS NULL) OR "
    "(state='PREPARATION_FAILED' AND preparation_reason IS NOT NULL AND "
    "preparation_reason IN ('ARGUMENTS_INVALID','STATE_FD_INVALID',"
    "'ASSIGNMENT_FILE_INVALID','ASSIGNMENT_HASH_MISMATCH','ASSIGNMENT_INVALID',"
    "'REAL_EXECUTION_BLOCKED')))) WITHOUT ROWID"
)
_PREPARATION_REASONS = frozenset({
    "ARGUMENTS_INVALID", "STATE_FD_INVALID", "ASSIGNMENT_FILE_INVALID",
    "ASSIGNMENT_HASH_MISMATCH", "ASSIGNMENT_INVALID", "REAL_EXECUTION_BLOCKED",
})
_REASONS = frozenset({
    "CONTROLLER_READY", "ARTIFACT_STORED", "PREPARATION_FAILED", "OUTCOME_UNKNOWN",
    "DATABASE_UNCONFIRMED", "ARGUMENTS_INVALID", "CONNECTION_INVALID", "SCHEMA_INVALID",
    "ASSIGNMENT_HASH_MISMATCH", "ASSIGNMENT_INVALID", "ASSIGNMENT_CONFLICT",
    "ASSIGNMENT_PIN_MISMATCH", "REAL_EXECUTION_BLOCKED", "JOB_NOT_FOUND", "STATE_CONFLICT",
})
_NO_DISCLOSURE = frozenset({"ASSIGNMENT_PIN_MISMATCH", "ASSIGNMENT_CONFLICT", "STATE_CONFLICT"})
_TRUST = (
    "input_origin_verified", "signer_roster_origin_verified", "issuer_environment_verified",
    "runtime_isolation_verified", "independent_review_verified", "trusted_gate_verified",
    "binding_verified", "native_ready", "merge_authorized",
)
_SCHEMA_SQL = (
    "SELECT CASE WHEN length(type)<=128 THEN CAST(type AS TEXT) END, "
    "CASE WHEN length(name)<=128 THEN CAST(name AS TEXT) END, "
    "CASE WHEN length(tbl_name)<=128 THEN CAST(tbl_name AS TEXT) END, "
    "CASE WHEN length(CAST(sql AS BLOB))<=4096 THEN CAST(sql AS TEXT) END "
    "FROM main.sqlite_master LIMIT 2"
)
_ROW_SQL = (
    "SELECT CASE WHEN typeof(job_id)='text' AND length(job_id) BETWEEN 1 AND 128 "
    "THEN CAST(job_id AS TEXT) END, "
    "CASE WHEN typeof(assignment_sha256)='text' AND length(assignment_sha256)=64 "
    "THEN CAST(assignment_sha256 AS TEXT) END, "
    "CASE WHEN typeof(state)='text' AND length(state)<=32 THEN CAST(state AS TEXT) END, "
    "CASE WHEN typeof(preparation_reason)='text' AND length(preparation_reason)<=64 "
    "THEN CAST(preparation_reason AS TEXT) END, preparation_reason IS NULL "
    "FROM main.fixture_controller_jobs WHERE job_id=? LIMIT 2"
)


class _Failure(Exception):
    def __init__(self, reason):
        self.reason = reason


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
        raise _Failure("CONNECTION_INVALID")
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


def _connection(connection):
    try:
        if (type(connection) is not sqlite3.Connection or connection.in_transaction
                or connection.isolation_level is not None or connection.row_factory is not None
                or connection.text_factory is not str):
            raise ValueError()
        busy = _one(connection, "PRAGMA busy_timeout")
        if (type(busy) is not int or not 0 <= busy <= 1000
                or _one(connection, "PRAGMA main.synchronous") not in (2, 3)
                or _one(connection, "PRAGMA main.journal_mode") not in ("delete", "memory")):
            raise ValueError()
        databases = _query(connection, "PRAGMA database_list")
        if (not databases or any(row[1] not in ("main", "temp") for row in databases)
                or sum(row[1] == "main" for row in databases) != 1
                or _query(connection, "SELECT 1 FROM temp.sqlite_master LIMIT 1")):
            raise ValueError()
    except Exception:
        raise _Failure("CONNECTION_INVALID") from None


def _preflight(ledger_connection, control_connection):
    if ledger_connection is control_connection:
        raise _Failure("ARGUMENTS_INVALID")
    # Check structural prerequisites of BOTH connections before issuing any SQL.
    for connection in (ledger_connection, control_connection):
        try:
            if (type(connection) is not sqlite3.Connection or connection.in_transaction
                    or connection.isolation_level is not None or connection.row_factory is not None
                    or connection.text_factory is not str):
                raise ValueError()
        except Exception:
            raise _Failure("CONNECTION_INVALID") from None
    _connection(ledger_connection)
    _connection(control_connection)


def _schema(connection, initialize):
    objects = _query(connection, _SCHEMA_SQL)
    app = _one(connection, "PRAGMA main.application_id")
    version = _one(connection, "PRAGMA main.user_version")
    if initialize and not objects and app == 0 and version == 0:
        _execute(connection, _DDL)
        _execute(connection, "PRAGMA main.application_id=1095779907")
        _execute(connection, "PRAGMA main.user_version=1")
        objects = _query(connection, _SCHEMA_SQL)
        app = _one(connection, "PRAGMA main.application_id")
        version = _one(connection, "PRAGMA main.user_version")
    if (type(app) is not int or app != _APPLICATION_ID or type(version) is not int or version != 1
            or objects != [("table", "fixture_controller_jobs", "fixture_controller_jobs", _DDL)]):
        raise _Failure("SCHEMA_INVALID")


def _job(value):
    return (type(value) is str and 1 <= len(value) <= 128
            and collector._JOB.fullmatch(value) is not None)


def _pin(value):
    return (type(value) is str and len(value) == 64
            and collector._SHA256.fullmatch(value) is not None)


def _read_control(connection, job_id):
    rows = _query(connection, _ROW_SQL, (job_id,))
    if not rows:
        return None
    if len(rows) != 1 or type(rows[0]) is not tuple or len(rows[0]) != 5:
        raise _Failure("STATE_CONFLICT")
    job, pin, state, reason, reason_null = rows[0]
    if (not _job(job) or job != job_id or not _pin(pin) or type(state) is not str
            or state not in ("CLAIMED", "PREPARATION_FAILED")
            or (state == "CLAIMED" and (reason is not None or reason_null != 1))
            or (state == "PREPARATION_FAILED" and
                (type(reason) is not str or reason not in _PREPARATION_REASONS or reason_null != 0))):
        raise _Failure("STATE_CONFLICT")
    return {"job_id": job, "assignment_sha256": pin, "state": state, "preparation_reason": reason}


def _state_run(connection, action, *, write=False, initialize=False):
    owned = False
    try:
        _connection(connection)
        owned = True
        _execute(connection, "BEGIN IMMEDIATE" if write else "BEGIN")
        _schema(connection, initialize)
        row, fresh = action()
        _commit(connection)
        if connection.in_transaction:
            raise _Failure("DATABASE_UNCONFIRMED")
        owned = False
        return "CONFIRMED", row, fresh
    except _Failure as error:
        reason = error.reason
    except Exception:
        reason = "DATABASE_UNCONFIRMED"
    finally:
        if owned:
            try:
                if connection.in_transaction:
                    _rollback(connection)
            except Exception:
                reason = "DATABASE_UNCONFIRMED"
    return reason, None, False


def _claim(connection, job_id, pin):
    def action():
        row = _read_control(connection, job_id)
        if row is not None:
            if row["assignment_sha256"] != pin:
                raise _Failure("STATE_CONFLICT")
            return row, False
        _execute(connection, "INSERT INTO main.fixture_controller_jobs VALUES (?,?,'CLAIMED',NULL)",
                 (job_id, pin))
        return _read_control(connection, job_id), True
    return _state_run(connection, action, write=True)


def _fail_preparation(connection, job_id, pin, reason):
    def action():
        row = _read_control(connection, job_id)
        if (row is None or row["assignment_sha256"] != pin
                or row["state"] != "CLAIMED" or reason not in _PREPARATION_REASONS):
            raise _Failure("STATE_CONFLICT")
        changed = _execute(connection,
                           "UPDATE main.fixture_controller_jobs SET state='PREPARATION_FAILED',"
                           "preparation_reason=? WHERE job_id=? AND assignment_sha256=? "
                           "AND state='CLAIMED' AND preparation_reason IS NULL", (reason, job_id, pin))
        if changed != 1:
            raise _Failure("STATE_CONFLICT")
        return _read_control(connection, job_id), False
    return _state_run(connection, action, write=True)


def _result(ledger_connection, control_connection, reason, control=None, record=None,
            artifact=None, collection_reason=None):
    if type(reason) is not str or reason not in _REASONS:
        reason = "OUTCOME_UNKNOWN"
    report = {
        "scope": "FIXTURE_CONTROLLER_ONLY",
        "status": "OBSERVED" if reason in ("CONTROLLER_READY", "ARTIFACT_STORED") else "BLOCKED",
        "reason": reason, "control_state": control["state"] if control else None,
        "preparation_reason": control["preparation_reason"] if control else None,
        "collection_reason": collection_reason,
        "artifact_storage_confirmed": reason == "ARTIFACT_STORED",
        "ledger_transaction_closed": _closed(ledger_connection),
        "control_transaction_closed": _closed(control_connection),
        "fixture_only": True, "head_code_executed": False,
        **{key: False for key in _TRUST},
    }
    if reason in _NO_DISCLOSURE:
        record, artifact = None, None
    return {"report": report, "record": record, "artifact": artifact}


def _ledger_reason(result):
    reason = result["report"]["reason"]
    if reason in _REASONS:
        return reason
    if reason in ("ROW_INVALID", "REVISION_CONFLICT", "ARTIFACT_CONFLICT"):
        return "STATE_CONFLICT"
    return "OUTCOME_UNKNOWN"


def initialize_fixture_controller(control_connection):
    """Initialize only an empty dedicated control DB; caller owns the connection."""
    reason, _, _ = _state_run(control_connection, lambda: (None, False), write=True, initialize=True)
    return _result(None, control_connection, "CONTROLLER_READY" if reason == "CONFIRMED" else reason)


def read_fixture_controller_job(ledger_connection, control_connection, job_id,
                                expected_assignment_sha256):
    """Observe original state/bytes without any state repair or child execution."""
    if not _job(job_id) or not _pin(expected_assignment_sha256):
        return _result(ledger_connection, control_connection, "ARGUMENTS_INVALID")
    try:
        _preflight(ledger_connection, control_connection)
        reason, control, _ = _state_run(control_connection,
                                       lambda: (_read_control(control_connection, job_id), False))
        if reason != "CONFIRMED":
            return _result(ledger_connection, control_connection, reason)
        observed = ledger.read_fixture_job(ledger_connection, job_id)
        if observed["report"]["reason"] != "JOB_OBSERVED":
            reason = _ledger_reason(observed)
            if reason == "JOB_NOT_FOUND" and control is not None:
                reason = "STATE_CONFLICT"
            return _result(ledger_connection, control_connection, reason)
        row = observed["record"]
        if row["assignment_sha256"] != expected_assignment_sha256:
            return _result(ledger_connection, control_connection, "ASSIGNMENT_PIN_MISMATCH")
        if control is not None and control["assignment_sha256"] != expected_assignment_sha256:
            return _result(ledger_connection, control_connection, "STATE_CONFLICT")
        if control is not None and control["state"] == "PREPARATION_FAILED":
            if row["revision"] != 0:
                return _result(ledger_connection, control_connection, "STATE_CONFLICT")
            return _result(ledger_connection, control_connection, "PREPARATION_FAILED", control, row)
        if row["revision"] == 1:
            outcome = json.loads(row["artifact_bytes"])["outcome"]
            return _result(ledger_connection, control_connection, "ARTIFACT_STORED", control, row,
                           row["artifact_bytes"], outcome["reason"])
        return _result(ledger_connection, control_connection, "OUTCOME_UNKNOWN", control, row)
    except _Failure as error:
        return _result(ledger_connection, control_connection, error.reason)
    except Exception:
        return _result(ledger_connection, control_connection, "DATABASE_UNCONFIRMED")


def _collection(result, row):
    """Check the pinned collector's complete response, not arbitrary worker claims."""
    try:
        if not collector._shape(result, ("report", "artifact", "binding")):
            raise ValueError()
        report, artifact = result["report"], result["artifact"]
        if type(report) is not dict or type(report.get("reason")) is not str:
            raise ValueError()
        reason = report["reason"]
        if reason not in collector._REASONS or type(report.get("assignment_bytes_verified")) is not bool:
            raise ValueError()
        boolean_keys = set(collector._report(reason)) - {
            "scope", "status", "reason", "returncode", "stdout_captured_bytes", "stderr_captured_bytes",
        }
        if any(type(report.get(key)) is not bool for key in boolean_keys):
            raise ValueError()
        if any(type(report.get(key)) is not int for key in ("stdout_captured_bytes", "stderr_captured_bytes")):
            raise ValueError()
        if report.get("returncode") is not None and type(report["returncode"]) is not int:
            raise ValueError()
        if artifact is None:
            expected = collector._report(reason, verified=report["assignment_bytes_verified"])
            if result["binding"] is not None or report != expected:
                raise ValueError()
            return reason, None, reason in _PREPARATION_REASONS
        if type(artifact) is not bytes or not 1 <= len(artifact) <= 16384:
            raise ValueError()
        parsed = json.loads(artifact)
        expected = collector._report(parsed["outcome"]["reason"], parsed, True)
        target = json.loads(row["assignment_bytes"])["target"]
        binding = dict(target, evidence_sha256=hashlib.sha256(artifact).hexdigest())
        if report != expected or type(result["binding"]) is not dict or result["binding"] != binding:
            raise ValueError()
        return reason, artifact, False
    except Exception:
        return None, None, False


def run_fixture_job(ledger_connection, control_connection, state_dir_fd,
                    expected_assignment_sha256, assignment_bytes):
    """Collect only after a NEW durable claim; all existing claims are read-only."""
    try:
        _preflight(ledger_connection, control_connection)
        reason, _, _ = _state_run(control_connection, lambda: (None, False))
        if reason != "CONFIRMED":
            return _result(ledger_connection, control_connection, reason)
        try:
            assignment = ledger._assignment(assignment_bytes, expected_assignment_sha256)
        except ledger._Failure as error:
            return _result(ledger_connection, control_connection, error.reason)
        reason, prior, _ = _state_run(control_connection,
                                     lambda: (_read_control(control_connection, assignment["job_id"]), False))
        if reason != "CONFIRMED":
            return _result(ledger_connection, control_connection, reason)
        if prior is not None:
            existing = ledger.read_fixture_job(ledger_connection, assignment["job_id"])
            if existing["report"]["reason"] == "JOB_NOT_FOUND":
                return _result(ledger_connection, control_connection, "STATE_CONFLICT")
            if existing["report"]["reason"] != "JOB_OBSERVED":
                return _result(ledger_connection, control_connection, _ledger_reason(existing))
            if (prior["assignment_sha256"] != existing["record"]["assignment_sha256"]
                    or (prior["state"] == "PREPARATION_FAILED" and existing["record"]["revision"] != 0)):
                return _result(ledger_connection, control_connection, "STATE_CONFLICT")
        assigned = ledger.assign_fixture_job(ledger_connection, expected_assignment_sha256, assignment_bytes)
        if assigned["report"]["reason"] not in ("ASSIGNED", "ASSIGNMENT_REPLAYED"):
            return _result(ledger_connection, control_connection, _ledger_reason(assigned))
        row = assigned["record"]
        job = row["job_id"]
        observed = read_fixture_controller_job(ledger_connection, control_connection, job,
                                               expected_assignment_sha256)
        if observed["report"]["reason"] != "OUTCOME_UNKNOWN":
            return observed
        reason, control, fresh = _claim(control_connection, job, expected_assignment_sha256)
        if reason != "CONFIRMED":
            return _result(ledger_connection, control_connection, reason)
        if not fresh:
            return read_fixture_controller_job(ledger_connection, control_connection, job,
                                               expected_assignment_sha256)
        # No connection owns a transaction while the child is collected.
        try:
            collected = collector.collect_fixture_assignment(state_dir_fd, expected_assignment_sha256)
        except Exception:
            return _result(ledger_connection, control_connection, "OUTCOME_UNKNOWN", control, row)
        collection_reason, artifact, preparation_failed = _collection(collected, row)
        if artifact is None:
            if not preparation_failed:
                return _result(ledger_connection, control_connection, "OUTCOME_UNKNOWN", control, row,
                               collection_reason=collection_reason)
            reason, _, _ = _fail_preparation(control_connection, job, expected_assignment_sha256,
                                             collection_reason)
            if reason != "CONFIRMED":
                return _result(ledger_connection, control_connection, reason,
                               collection_reason=collection_reason)
            return read_fixture_controller_job(ledger_connection, control_connection, job,
                                               expected_assignment_sha256)
        recorded = ledger.record_fixture_artifact(ledger_connection, job, expected_assignment_sha256, 0, artifact)
        if recorded["report"]["reason"] not in ("ARTIFACT_RECORDED", "ARTIFACT_REPLAYED"):
            return _result(ledger_connection, control_connection, _ledger_reason(recorded), control,
                           artifact=artifact, collection_reason=collection_reason)
        observed = read_fixture_controller_job(ledger_connection, control_connection, job,
                                               expected_assignment_sha256)
        if (observed["report"]["reason"] != "ARTIFACT_STORED"
                and observed["report"]["reason"] not in _NO_DISCLOSURE):
            observed["artifact"] = artifact
        return observed
    except _Failure as error:
        return _result(ledger_connection, control_connection, error.reason)
    except Exception:
        return _result(ledger_connection, control_connection, "DATABASE_UNCONFIRMED")
