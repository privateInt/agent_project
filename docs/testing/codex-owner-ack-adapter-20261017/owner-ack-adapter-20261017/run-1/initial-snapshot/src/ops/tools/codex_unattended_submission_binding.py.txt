"""Persist immutable intended queue parameters before an external claim/send.

Borrow two distinct dedicated file SQLite connections. Explicit initialization
only; no paths, connect/close, migrations, claims, remote calls or retries.
The intent lock protects the local PREPARED snapshot through binding commit.
Caller serialization after return, provenance and actual dispatch remain external.
"""
import json
import sqlite3

from src.ops.tools import codex_unattended_intent_journal as _journal
from src.ops.tools import codex_unattended_queue_observer as _parser
from src.ops.tools import github_fixture_collector as _json
from src.ops.tools import github_fixture_ledger as _sqlite


_APPLICATION_ID = 0x41494231
_DDL = (
    "CREATE TABLE submission_bindings (project_id TEXT NOT NULL, batch_id TEXT NOT NULL, "
    "task_id TEXT NOT NULL, binding_bytes BLOB NOT NULL, binding_sha256 TEXT NOT NULL, "
    "PRIMARY KEY(project_id,batch_id,task_id)) WITHOUT ROWID"
)
_SCHEMA_SQL = (
    "SELECT CASE WHEN length(type)<=128 THEN CAST(type AS TEXT) END, "
    "CASE WHEN length(name)<=128 THEN CAST(name AS TEXT) END, "
    "CASE WHEN length(tbl_name)<=128 THEN CAST(tbl_name AS TEXT) END, "
    "CASE WHEN length(CAST(sql AS BLOB))<=4096 THEN CAST(sql AS TEXT) END "
    "FROM main.sqlite_master ORDER BY type,name LIMIT 2"
)
_ROW_SQL = (
    "SELECT " + ", ".join(
        "CASE WHEN typeof(" + key + ")='text' AND length(" + key + ") BETWEEN 1 AND 128 "
        "THEN CAST(" + key + " AS TEXT) END" for key in ("project_id", "batch_id", "task_id")
    ) + ", CASE WHEN typeof(binding_bytes)='blob' AND length(binding_bytes) BETWEEN 1 AND 4096 "
    "THEN CAST(binding_bytes AS BLOB) END, "
    "CASE WHEN typeof(binding_sha256)='text' AND length(binding_sha256)=64 "
    "THEN CAST(binding_sha256 AS TEXT) END "
    "FROM main.submission_bindings WHERE project_id=? AND batch_id=? AND task_id=? LIMIT 2"
)
_FIELDS = frozenset((
    "schema", "project_id", "batch_id", "task_id", "intent_sha256", "intent_revision",
    "client_user_message_id", "thread_id", "submission_params_sha256",
))
_CONFIRMED = frozenset(("BINDINGS_READY", "BINDING_STORED", "BINDING_REUSED", "BINDING_OBSERVED"))
_BLOCKED = frozenset((
    "ARGUMENTS_INVALID", "CONNECTION_INVALID", "DATABASE_NOT_EMPTY", "SCHEMA_INVALID",
    "INPUT_HASH_MISMATCH", "INPUT_INVALID", "INTENT_BLOCKED", "INTENT_NOT_PREPARED",
    "INTENT_BINDING_MISMATCH", "BINDING_NOT_FOUND", "BINDING_PIN_MISMATCH",
    "BINDING_CONFLICT", "ROW_INVALID",
))
_query, _execute, _one = _sqlite._query, _sqlite._execute, _sqlite._one


class _Failure(Exception):
    def __init__(self, reason):
        self.reason = reason


def _need(condition, reason):
    if not condition:
        raise _Failure(reason)


def _commit(connection):
    _execute(connection, "COMMIT")


def _rollback(connection):
    _execute(connection, "ROLLBACK")


def _structural(connection):
    try:
        valid = (type(connection) is sqlite3.Connection and not connection.in_transaction
                 and connection.isolation_level is None and connection.row_factory is None
                 and connection.text_factory is str)
    except Exception:
        valid = False
    _need(valid, "CONNECTION_INVALID")


def _profile(connection):
    try:
        _journal._connection(connection)
    except _journal._Failure as error:
        if error.reason == "CONNECTION_INVALID":
            raise _Failure("CONNECTION_INVALID") from None
        raise


def _filename(connection):
    rows = _query(connection, "PRAGMA database_list")
    return next(row[2] for row in rows if row[1] == "main")


def _schema(connection, initialize):
    objects = _query(connection, _SCHEMA_SQL)
    app = _one(connection, "PRAGMA main.application_id")
    version = _one(connection, "PRAGMA main.user_version")
    if initialize:
        _need(not objects and app == 0 and version == 0, "DATABASE_NOT_EMPTY")
        _execute(connection, _DDL)
        _execute(connection, "PRAGMA main.application_id=1095320113")
        _execute(connection, "PRAGMA main.user_version=1")
        objects = _query(connection, _SCHEMA_SQL)
        app = _one(connection, "PRAGMA main.application_id")
        version = _one(connection, "PRAGMA main.user_version")
    _need(type(app) is int and app == _APPLICATION_ID and type(version) is int and version == 1
          and objects == [("table", "submission_bindings", "submission_bindings", _DDL)],
          "SCHEMA_INVALID")


def _binding(raw, expected):
    try:
        _need(type(raw) is bytes and 1 <= len(raw) <= 4096 and _journal._hex(expected)
              and _parser._sha(raw) == expected, "ROW_INVALID")
        value = _parser._decode(raw, "ROW_INVALID")
        _need(set(value) == _FIELDS and type(value["schema"]) is int and value["schema"] == 1
              and type(value["intent_revision"]) is int and value["intent_revision"] == 0,
              "ROW_INVALID")
        _need(_journal._key(tuple(value[k] for k in ("project_id", "batch_id", "task_id")))
              and _journal._identifier(value["client_user_message_id"])
              and _parser._identifier(value["thread_id"])
              and _journal._hex(value["intent_sha256"])
              and _journal._hex(value["submission_params_sha256"])
              and _json._canonical(value) == raw, "ROW_INVALID")
        return value
    except Exception:
        raise _Failure("ROW_INVALID") from None


def _read(connection, key):
    rows = _query(connection, _ROW_SQL, key)
    if not rows:
        return None
    _need(len(rows) == 1 and type(rows[0]) is tuple and len(rows[0]) == 5, "ROW_INVALID")
    project, batch, task, raw, pin = rows[0]
    value = _binding(raw, pin)
    _need((project, batch, task) == key and _journal._key((project, batch, task))
          and tuple(value[k] for k in ("project_id", "batch_id", "task_id")) == key, "ROW_INVALID")
    return {"binding_bytes": raw, "binding_sha256": pin}, value


def _related(value, intent, pin):
    _need(value["intent_sha256"] == pin
          and value["client_user_message_id"] == intent["client_user_message_id"],
          "INTENT_BINDING_MISMATCH")


def _submission(raw, pin, intent_pin):
    try:
        _parser._inputs(raw, pin, (), intent_pin)
        value = _parser._decode(raw, "INPUT_INVALID")
        _need(set(value) == {"threadId", "clientUserMessageId", "input"}
              and _parser._identifier(value["threadId"])
              and _parser._identifier(value["clientUserMessageId"]), "INPUT_INVALID")
        text = _parser._text(value["input"], "INPUT_INVALID")
        return value, _parser._sha(text)
    except _parser._Failure as error:
        raise _Failure(error.reason) from None


def _report():
    return {
        "scope": "DURABLE_SUBMISSION_BINDING_ONLY", "status": "UNCERTAIN",
        "reason": "CHECKPOINT_UNCONFIRMED", "phase": "PRECHECK",
        "database_commit_confirmed": False, "intent_validated": False, "intent_reason": None,
        "intent_state": None, "intent_revision": None, "new_binding": False,
        "binding_transaction_closed": False, "intent_transaction_closed": None,
        "remote_io_performed": False, "remote_mutation_authorized": False,
        "input_origin_verified": False, "native_acceptance_verified": False,
        "replay_permitted": False, "native_ready": False,
    }


def _result(report, intent_connection, binding_connection, reason, record=None, *, initialize=False):
    if reason not in _CONFIRMED | _BLOCKED:
        reason = "CHECKPOINT_UNCONFIRMED"
    confirmed = reason in _CONFIRMED
    report.update(
        reason=reason,
        status="CONFIRMED" if confirmed else "BLOCKED" if reason in _BLOCKED else "UNCERTAIN",
        database_commit_confirmed=confirmed, new_binding=confirmed and reason == "BINDING_STORED",
        binding_transaction_closed=_sqlite._closed(binding_connection),
        intent_transaction_closed=None if initialize else _sqlite._closed(intent_connection),
    )
    return {"report": report, "record": record if confirmed else None}


def _run(intent_connection, binding_connection, mode, key=None, pin=None, submission=None,
         submission_pin=None, text_pin=None, expected_binding=None):
    report, record = _report(), None
    owned_intent, owned_binding = False, False
    reason = "CHECKPOINT_UNCONFIRMED"
    try:
        # Check BOTH caller transaction states before any profile SQL.
        _structural(binding_connection)
        if mode != "initialize":
            _structural(intent_connection)
            _need(intent_connection is not binding_connection, "CONNECTION_INVALID")
        _profile(binding_connection)
        if mode != "initialize":
            _profile(intent_connection)
            _need(_filename(intent_connection) != _filename(binding_connection), "CONNECTION_INVALID")
            report["phase"] = "INTENT_LOCK"
            owned_intent = True
            _execute(intent_connection, "BEGIN IMMEDIATE" if mode == "bind" else "BEGIN")
            report["phase"] = "INTENT_VALIDATE"
            try:
                _journal._schema(intent_connection, False)
                intent = _journal._pinned(intent_connection, key, pin)
            except _journal._Failure as error:
                if error.reason in _journal._BLOCKED:
                    report["intent_reason"] = error.reason
                    raise _Failure("INTENT_BLOCKED") from None
                raise
            report.update(intent_validated=True, intent_state=intent["state"],
                          intent_revision=intent["revision"])
            if mode == "bind":
                # D38 M_B: this guard is the local pre-claim ordering invariant.
                _need(intent["state"] == "PREPARED" and intent["revision"] == 0,
                      "INTENT_NOT_PREPARED")
        report["phase"] = "BINDING_TRANSACTION"
        owned_binding = True
        _execute(binding_connection, "BEGIN" if mode == "read" else "BEGIN IMMEDIATE")
        report["phase"] = "BINDING_VALIDATE"
        _schema(binding_connection, mode == "initialize")
        if mode == "initialize":
            reason = "BINDINGS_READY"
        else:
            stored = _read(binding_connection, key)
            if stored is not None:
                _related(stored[1], intent, pin)
            if mode == "read":
                _need(stored is not None, "BINDING_NOT_FOUND")
                _need(stored[0]["binding_sha256"] == expected_binding, "BINDING_PIN_MISMATCH")
                reason, record = "BINDING_OBSERVED", stored[0]
            else:
                value = dict(
                    schema=1, project_id=key[0], batch_id=key[1], task_id=key[2],
                    intent_sha256=pin, intent_revision=0,
                    client_user_message_id=intent["client_user_message_id"],
                    thread_id=submission["threadId"], submission_params_sha256=submission_pin,
                )
                raw = _json._canonical(value)
                expected = {"binding_bytes": raw, "binding_sha256": _parser._sha(raw)}
                if stored is not None:
                    _need(stored[0] == expected, "BINDING_CONFLICT")
                intent_value = json.loads(intent["intent_bytes"].decode("utf-8"))
                _need(submission["clientUserMessageId"] == intent["client_user_message_id"]
                      and text_pin == intent_value["prompt_sha256"], "INTENT_BINDING_MISMATCH")
                if stored is None:
                    report["phase"] = "BINDING_WRITE"
                    _execute(binding_connection, "INSERT INTO main.submission_bindings VALUES (?,?,?,?,?)",
                             (*key, raw, expected["binding_sha256"]))
                    report["phase"] = "BINDING_READBACK"
                    after = _read(binding_connection, key)
                    _need(after is not None and after[0] == expected, "CHECKPOINT_UNCONFIRMED")
                    reason, record = "BINDING_STORED", after[0]
                else:
                    reason, record = "BINDING_REUSED", stored[0]
        report["phase"] = "BINDING_COMMIT"
        _commit(binding_connection)
        _need(not binding_connection.in_transaction, "CHECKPOINT_UNCONFIRMED")
        owned_binding = False
        if owned_intent:
            report["phase"] = "INTENT_RELEASE"
            _rollback(intent_connection)
            _need(not intent_connection.in_transaction, "CHECKPOINT_UNCONFIRMED")
            owned_intent = False
        report["phase"] = "COMPLETE"
    except _Failure as error:
        reason, record = error.reason, None
    except Exception:
        reason, record = "CHECKPOINT_UNCONFIRMED", None
    finally:
        for connection, owned in ((binding_connection, owned_binding),
                                  (intent_connection, owned_intent)):
            if owned:
                try:
                    if connection.in_transaction:
                        _rollback(connection)
                except Exception:
                    reason, record = "CHECKPOINT_UNCONFIRMED", None
    return _result(report, intent_connection, binding_connection, reason, record,
                   initialize=mode == "initialize")


def initialize_submission_bindings(binding_connection):
    """Explicitly initialize an empty, dedicated binding DB; never migrate."""
    return _run(None, binding_connection, "initialize")


def bind_submission(intent_connection, binding_connection, key, expected_intent_sha256, *,
                    submission_params_bytes, expected_submission_sha256):
    """Commit an immutable intended thread/params binding before an external claim."""
    try:
        _need(_journal._key(key) and _journal._hex(expected_intent_sha256), "ARGUMENTS_INVALID")
        value, text_pin = _submission(submission_params_bytes, expected_submission_sha256,
                                      expected_intent_sha256)
    except _Failure as error:
        return _result(_report(), intent_connection, binding_connection, error.reason)
    except Exception:
        return _result(_report(), intent_connection, binding_connection, "INPUT_INVALID")
    return _run(intent_connection, binding_connection, "bind", key, expected_intent_sha256,
                value, expected_submission_sha256, text_pin)


def read_submission_binding(intent_connection, binding_connection, key,
                            expected_intent_sha256, expected_binding_sha256):
    """Read exact stored bytes/pins even after UNKNOWN; never rearm or replay."""
    if not (_journal._key(key) and _journal._hex(expected_intent_sha256)
            and _journal._hex(expected_binding_sha256)):
        return _result(_report(), intent_connection, binding_connection, "ARGUMENTS_INVALID")
    return _run(intent_connection, binding_connection, "read", key, expected_intent_sha256,
                expected_binding=expected_binding_sha256)
