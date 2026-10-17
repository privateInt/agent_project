"""Owner ACK frozen v3: four new tests, pure adapter plus temporary PR21 composition."""
import contextlib
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from src.ops.tools import codex_unattended_owner_ack_adapter as product
from src.ops.tools import codex_unattended_queue_receipts as receipt
from src.ops.tools import codex_unattended_submission_binding as binding
from src.ops.tools import codex_unattended_intent_journal as journal
from src.ops.tools import codex_unattended_quota_reader as quota
from src.ops.tools.tests import test_codex_unattended_queue_receipts as receipt_support
from src.ops.tools.tests import test_codex_unattended_intent_journal as support
from src.ops.tools.tests import test_codex_unattended_submission_binding as binding_support
from src.ops.tools.tests import test_codex_unattended_quota_reader as quota_support

FIXTURES = Path(__file__).resolve().parent / "fixtures/codex_unattended_owner_ack_adapter"
FALSE_FLAGS = {"remote_io_performed", "db_io_performed", "input_origin_verified", "client_identity_verified",
               "transport_pin_verified", "transport_id_matching_verified", "deadline_verified",
               "native_acceptance_verified", "remote_mutation_authorized", "dispatch_permitted", "replay_permitted", "native_ready"}
REPORT_KEYS = {"scope", "ready", "status", "reason", "ack_result_bytes", "expected_ack_sha256", "call_error_code"} | FALSE_FLAGS
PREPARED = {"ACK_RESULT_PREPARED", "ABSENCE_PREPARED", "CALL_ERROR_PREPARED"}
REJECTED = {"INPUTS_MUTUALLY_EXCLUSIVE", "RESULT_INVALID", "TEXT_UTF8_LIMIT", "ERROR_INVALID", "PREPARATION_UNCONFIRMED"}
CODES = frozenset(quota._ERRORS) | {"UNCLASSIFIED_LOCAL_OR_TRANSPORT_ERROR"}
OBSERVATIONS = []
COMPOSITIONS = []
CLEANUPS = []
PRIVATE = "PRIVATE_SYNTHETIC_OWNER_ADAPTER_EXCEPTION_NEVER_LOG"


def old_case_codes():
    codes = set(receipt_support.old_case_codes())
    for value in receipt_support.QueueReceiptTests.__dict__.values():
        function = getattr(value, "__func__", value)
        while hasattr(function, "__wrapped__"):
            function = function.__wrapped__
        code = getattr(function, "__code__", None)
        if code is not None:
            codes.add(code)
    return codes


def valid_identity(value):
    item = value["queuedSubmission"]
    text = item["input"][0]
    return (id(value), id(item), id(item["input"]), id(text),
            id(text["text_elements"]) if "text_elements" in text else None,
            tuple(value), tuple(item), tuple(text))


@contextlib.contextmanager
def temporary_receipt_db(template, text, client):
    """Only fixture code creates/initializes/claims/closes the three temporary DBs."""
    with tempfile.TemporaryDirectory(prefix="owner-adapter-db-") as directory:
        root = Path(directory)
        paths = {role: root / (role + ".sqlite") for role in ("intent", "binding", "receipt")}
        original = {role: support.connect(path) for role, path in paths.items()}
        peers = {role: support.connect(path) for role, path in paths.items()}
        db = SimpleNamespace(**original, **{role + "_path": path for role, path in paths.items()},
                             **{"peer_" + role: c for role, c in peers.items()})
        value = copy.deepcopy(template["intent"])
        value["clientUserMessageId"], value["prompt_sha256"] = client, support.sha(text.encode("utf-8"))
        db.key, db.intent_raw = support.intent_key(value), support.canonical(value)
        db.intent_sha = support.sha(db.intent_raw)
        db.params = {"threadId": template["thread_id"], "clientUserMessageId": client,
                     "input": [{"type": "text", "text": text}]}
        db.raw = support.canonical(db.params)
        try:
            with quota_support.strict_io() as (attempts, output):
                results = [(journal.initialize_intent_journal(db.intent), "JOURNAL_READY"),
                           (journal.reserve_intent(db.intent, db.intent_sha, db.intent_raw), "INTENT_STORED"),
                           (binding.initialize_submission_bindings(db.binding), "BINDINGS_READY")]
                bound = binding.bind_submission(db.intent, db.binding, db.key, db.intent_sha,
                    submission_params_bytes=db.raw, expected_submission_sha256=support.sha(db.raw))
                results.extend(((bound, "BINDING_STORED"),
                                (journal.claim_intent(db.intent, db.key, db.intent_sha, 0), "CLAIM_RECORDED"),
                                (receipt.initialize_queue_receipts(db.receipt), "RECEIPTS_READY")))
            if attempts or output.getvalue():
                raise AssertionError("FIXTURE_EXTERNAL_IO_FORBIDDEN")
            if any(result["report"]["reason"] != reason for result, reason in results):
                raise AssertionError("PINNED_SOURCE_FIXTURE_SETUP_FAILED")
            db.binding_sha = bound["record"]["binding_sha256"]
            yield db
        finally:
            seen, closed = set(), []
            for connection in (db.peer_receipt, db.peer_binding, db.peer_intent,
                               db.receipt, db.binding, db.intent, *peers.values(), *original.values()):
                if id(connection) in seen:
                    continue
                seen.add(id(connection))
                closed.append(connection)
                if binding_support.alive(connection):
                    connection.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
                    if connection.in_transaction:
                        connection.rollback()
                    connection.close()
            all_closed = all(not binding_support.alive(connection) for connection in closed)
            CLEANUPS.append({"distinct_owned_connections": len(closed), "all_closed": all_closed,
                             "identity_deduplicated": len(seen) == len(closed), "current_reopened_connections_included": True})
            if not all_closed:
                raise AssertionError("OWNED_FIXTURE_CONNECTION_LEAK")


class HookValue:
    def __init__(self, counts):
        self.counts = counts

    def denied(self, *args, **kwargs):
        self.counts["hooks"] += 1
        raise support.ForbiddenIO("HOSTILE_VALUE_HOOK_CALLED")

    __str__ = __repr__ = __bool__ = __eq__ = __hash__ = denied


class CollidingKey:
    def __init__(self, counts):
        self.counts, self.armed = counts, False

    def __hash__(self):
        self.counts["hash"] += 1
        if self.armed:
            raise support.ForbiddenIO("HOSTILE_KEY_HASH_CALLED")
        return hash("code")

    def __eq__(self, other):
        self.counts["equality"] += 1
        raise support.ForbiddenIO("HOSTILE_KEY_EQUALITY_CALLED")


class HostileError(Exception):
    def __getattribute__(self, name):
        raise support.ForbiddenIO("ORIGINAL_EXCEPTION_ATTRIBUTE_READ")

    def __str__(self):
        raise support.ForbiddenIO("ORIGINAL_EXCEPTION_STRINGIFIED")

    __repr__ = __str__

    @property
    def code(self):
        raise support.ForbiddenIO("ORIGINAL_EXCEPTION_PROPERTY_READ")


class NoClassObject:
    @property
    def __class__(self):
        raise support.ForbiddenIO("NON_EXCEPTION_CLASS_PROPERTY_READ")


class DictSubclass(dict):
    def keys(self):
        raise support.ForbiddenIO("DICT_SUBCLASS_HOOK")

    def __iter__(self):
        raise support.ForbiddenIO("DICT_SUBCLASS_ITERATOR")


class ListSubclass(list):
    def __len__(self):
        raise support.ForbiddenIO("LIST_SUBCLASS_LENGTH")


class StrSubclass(str):
    def __eq__(self, other):
        raise support.ForbiddenIO("STR_SUBCLASS_EQUALITY")

    __hash__ = str.__hash__

    def encode(self, *args, **kwargs):
        raise support.ForbiddenIO("STR_SUBCLASS_ENCODE")


class OwnerAckAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        manifest = json.loads((FIXTURES / "manifest.json").read_bytes())
        if not manifest["fixture_only"]:
            raise AssertionError("SYNTHETIC_FIXTURE_REQUIRED")
        for name, pin in manifest["files"].items():
            if support.sha((FIXTURES / name).read_bytes()) != pin:
                raise AssertionError("RELATIVE_FIXTURE_PIN_MISMATCH")
        if manifest["files"]["ThreadQueueAddResponse.json"] != "c11b9772788427b19c9f9decf0765e2b41cbdead1aa9c3d39f42fdd70b339314":
            raise AssertionError("GENERATED_SCHEMA_PIN_MISMATCH")
        cls.data = json.loads((FIXTURES / "synthetic.json").read_bytes())
        if cls.data["provenance"] != "SYNTHETIC_ONLY_OWNER_ACK_ADAPTER_NO_NATIVE_IO":
            raise AssertionError("SYNTHETIC_FIXTURE_REQUIRED")
        if support.sha(support.canonical(cls.data["intent"])) != cls.data["canonical_intent_sha256"]:
            raise AssertionError("SYNTHETIC_INTENT_PIN_MISMATCH")

    def setUp(self):
        self.helper_markers, self.helper_invocations = {}, {}
        self.target_input = None
        self.target_context = None

    def payload(self, text=None, *, client=None, queued=None, spans=False):
        item = {"type": "text", "text": self.data["prompt_fixture"] if text is None else text}
        if spans:
            item["text_elements"] = []
        return {"queuedSubmission": {"id": queued or self.data["queued_id"],
                "clientUserMessageId": client or self.data["intent"]["clientUserMessageId"], "input": [item]}}

    def invoke(self, *, reason, **arguments):
        previous = sys.getprofile()
        forbidden = old_case_codes() | {getattr(module, name).__code__ for module, names in (
            (journal, ("initialize_intent_journal", "reserve_intent", "read_intent", "claim_intent")),
            (binding, ("initialize_submission_bindings", "bind_submission", "read_submission_binding")),
            (receipt, ("initialize_queue_receipts", "record_queue_receipt", "read_queue_receipt")),
            (quota, ("read_quota_only",))) for name in names}

        def watch(frame, event, arg):
            if event == "call" and frame.f_code in forbidden:
                raise support.ForbiddenIO("ADAPTER_SOURCE_API_OR_OLD_CASE_CALL_FORBIDDEN")

        try:
            sys.setprofile(watch)
            with quota_support.strict_io() as (attempts, output):
                report = product.prepare_queue_ack(**arguments)
        finally:
            sys.setprofile(previous)
        self.last_report = report
        raw = report.get("ack_result_bytes") if type(report) is dict else None
        observation = {"ready": report.get("ready"), "status": report.get("status"), "reason": report.get("reason"),
                       "ack_bytes_length": len(raw) if type(raw) is bytes else None,
                       "actual_sha256": support.sha(raw) if type(raw) is bytes else None,
                       "expected_ack_sha256": report.get("expected_ack_sha256"), "call_error_code": report.get("call_error_code"),
                       "utf8_length": None, "input_unchanged": None, "runtime_calls": 0}
        if type(raw) is bytes:
            observation["utf8_length"] = len(json.loads(raw)["queuedSubmission"]["input"][0]["text"].encode("utf-8"))
        if self.target_input is not None:
            observation["input_unchanged"] = (support.canonical(self.target_input) == self.target_original
                                               and valid_identity(self.target_input) == self.target_identity)
        self.last_observation = observation
        self.assertEqual([], attempts)
        self.assertFalse(bool(output.getvalue()), "CAPTURED_OUTPUT_MUST_BE_EMPTY")
        self.assertIs(type(report), dict)
        self.assertEqual(REPORT_KEYS, set(report))
        self.assertEqual("OWNER_QUEUE_ACK_ADAPTER_ONLY", report["scope"])
        self.assertIn(report["reason"], PREPARED | REJECTED)
        self.assertIs(type(report["ready"]), bool)
        self.assertIs(report["ready"], report["reason"] in PREPARED)
        self.assertEqual("PREPARED" if report["ready"] else "REJECTED", report["status"])
        for flag in FALSE_FLAGS:
            self.assertIs(report[flag], False)
        if report["reason"] == "ACK_RESULT_PREPARED":
            self.assertIs(type(raw), bytes)
            self.assertTrue(1 <= len(raw) <= 262144)
            self.assertEqual(support.sha(raw), report["expected_ack_sha256"])
            self.assertTrue(raw == support.canonical(json.loads(raw)), "CANONICAL_ACK_BYTES_REQUIRED")
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertIsNone(report["call_error_code"])
        else:
            self.assertIsNone(raw)
            self.assertIsNone(report["expected_ack_sha256"])
            if report["reason"] == "CALL_ERROR_PREPARED":
                self.assertIs(type(report["call_error_code"]), str)
                self.assertIn(report["call_error_code"], CODES)
            else:
                self.assertIsNone(report["call_error_code"])
        public = support.canonical({key: value for key, value in report.items() if key != "ack_result_bytes"})
        for private in (PRIVATE, support.PRIVATE_ERROR, self.data["prompt_fixture"]):
            self.assertNotIn(private.encode("utf-8"), public)
        if self.target_input is not None:
            self.assertIs(observation["input_unchanged"], True)
        OBSERVATIONS.append(observation)
        # Both mutants must fail here after the actual wrong transient bytes/hash were captured.
        self.assertEqual(reason, report["reason"])
        return report

    def compose_receipt(self, prepared, *, text=None, client=None, outcome):
        self.assertIs(prepared["ready"], True)  # Rejected None fields can never be forwarded as absence.
        text = self.data["prompt_fixture"] if text is None else text
        client = self.data["intent"]["clientUserMessageId"] if client is None else client
        with temporary_receipt_db(self.data, text, client) as db:
            self.assertEqual(("SUBMISSION_UNKNOWN", 1), tuple(support.rows(db.intent)[0][-2:]))
            sources = (support.database_snapshot(db.intent, db.intent_path),
                       binding_support.binding_snapshot(db.binding, db.binding_path))
            profiles = [support.profile(c) for c in (db.intent, db.binding, db.receipt)]
            with quota_support.strict_io() as (attempts, output):
                stored = receipt.record_queue_receipt(db.intent, db.binding, db.receipt, db.key, db.intent_sha, db.binding_sha,
                    submission_params_bytes=db.raw, expected_submission_sha256=support.sha(db.raw),
                    ack_result_bytes=prepared["ack_result_bytes"], expected_ack_sha256=prepared["expected_ack_sha256"],
                    call_error_code=prepared["call_error_code"])
            self.assertEqual([], attempts)
            self.assertFalse(bool(output.getvalue()), "CAPTURED_OUTPUT_MUST_BE_EMPTY")
            self.assertEqual("RECEIPT_STORED", stored["report"]["reason"])
            self.assertEqual(outcome, stored["report"]["outcome"])
            for flag in receipt_support.FALSE_FLAGS:
                self.assertIs(stored["report"][flag], False)
            record = stored["record"]
            self.assertEqual(support.sha(record["receipt_bytes"]), record["receipt_sha256"])
            projection = json.loads(record["receipt_bytes"])
            expected_projection = {"schema": 1, "project_id": db.key[0], "batch_id": db.key[1], "task_id": db.key[2],
                "intent_sha256": db.intent_sha, "binding_sha256": db.binding_sha,
                "thread_id": self.data["thread_id"], "client_user_message_id": client,
                "submission_params_sha256": support.sha(db.raw), "ack_result_sha256": prepared["expected_ack_sha256"],
                "outcome": outcome,
                "observation_reason": prepared["call_error_code"] or ("ACK_RESULT_OBSERVED" if outcome == "ACK_CANDIDATE" else "ACK_RESULT_ABSENT"),
                "queued_submission_id": json.loads(prepared["ack_result_bytes"])["queuedSubmission"]["id"] if outcome == "ACK_CANDIDATE" else None}
            self.assertEqual(expected_projection, projection)
            self.assertTrue(support.canonical(expected_projection) == record["receipt_bytes"], "EXACT_RECEIPT_PROJECTION_BYTES_REQUIRED")
            self.assertEqual(db.intent_sha, projection["intent_sha256"])
            self.assertEqual(db.binding_sha, projection["binding_sha256"])
            self.assertEqual(support.sha(db.raw), projection["submission_params_sha256"])
            self.assertEqual(prepared["expected_ack_sha256"], projection["ack_result_sha256"])
            self.assertEqual(prepared["call_error_code"] or ("ACK_RESULT_OBSERVED" if outcome == "ACK_CANDIDATE" else "ACK_RESULT_ABSENT"), projection["observation_reason"])
            self.assertEqual([(*db.key, record["receipt_bytes"], record["receipt_sha256"])], receipt_support.receipt_rows(db.peer_receipt))
            for role in ("intent", "binding", "receipt"):
                getattr(db, role).close()
                setattr(db, role, support.connect(getattr(db, role + "_path")))
            with quota_support.strict_io() as (attempts, output):
                observed = receipt.read_queue_receipt(db.receipt, db.key, db.intent_sha, db.binding_sha, record["receipt_sha256"])
            self.assertEqual([], attempts)
            self.assertFalse(bool(output.getvalue()), "CAPTURED_OUTPUT_MUST_BE_EMPTY")
            self.assertEqual("RECEIPT_OBSERVED", observed["report"]["reason"])
            self.assertTrue(record == observed["record"], "REOPEN_RECEIPT_BYTES_OR_PIN_CHANGED")
            self.assertEqual(profiles, [support.profile(c) for c in (db.intent, db.binding, db.receipt)])
            self.assertTrue(sources == (support.database_snapshot(db.intent, db.intent_path),
                                        binding_support.binding_snapshot(db.binding, db.binding_path)), "SOURCE_DATABASE_CHANGED")
            self.assertFalse(text.encode("utf-8") in db.receipt_path.read_bytes(), "PROMPT_MUST_NOT_BE_STORED_IN_RECEIPT_DB")
            if prepared["ack_result_bytes"] is not None:
                self.assertFalse(prepared["ack_result_bytes"] in db.receipt_path.read_bytes(), "RAW_ACK_MUST_NOT_BE_STORED_IN_RECEIPT_DB")
            for connection in (db.intent, db.binding, db.receipt, db.peer_intent, db.peer_binding, db.peer_receipt):
                self.assertTrue(support.transaction_closed(connection))
            COMPOSITIONS.append({"outcome": outcome, "observation_reason": projection["observation_reason"],
                "temporary_file_databases": 3, "peer_exact_row": True, "all_three_closed_reopened": True,
                "receipt_projection": projection, "receipt_sha256": record["receipt_sha256"],
                "actual_bytes_sha256": support.sha(observed["record"]["receipt_bytes"]), "source_bytes_unchanged": True,
                "borrowed_profiles_unchanged": True, "transactions_closed": True, "raw_prompt_and_ack_absent": True})

    def case_utf8_byte_cap_rejects_overlong_result(self):
        name = "case_utf8_byte_cap_rejects_overlong_result"
        self.helper_invocations[name] = self.helper_invocations.get(name, 0) + 1
        value = self.payload(self.data["unicode_character"] * 4097)
        self.target_input, self.target_original, self.target_identity = value, support.canonical(value), valid_identity(value)
        self.target_context = {"has_result": True, "has_error": False, "expected_payload_sha256": support.sha(self.target_original), "utf8_length": 16388}
        try:
            with mock.patch.object(product, "_encode_text", wraps=product._encode_text) as encoder, \
                    mock.patch.object(product, "_canonical", wraps=product._canonical) as serializer:
                self.invoke(decoded_result=value, reason="TEXT_UTF8_LIMIT")
            self.assertEqual(1, encoder.call_count)
            self.assertEqual(0, serializer.call_count)
            self.helper_markers[name] = {"outcome": "PASS", "invocations": self.helper_invocations[name]}
        finally:
            self.target_input = None

    def case_dual_arguments_are_rejected(self):
        name = "case_dual_arguments_are_rejected"
        self.helper_invocations[name] = self.helper_invocations.get(name, 0) + 1
        value = self.payload()
        self.target_input, self.target_original, self.target_identity = value, support.canonical(value), valid_identity(value)
        self.target_context = {"has_result": True, "has_error": True, "expected_payload_sha256": support.sha(self.target_original)}
        try:
            self.invoke(decoded_result=value, call_error=Exception(PRIVATE), reason="INPUTS_MUTUALLY_EXCLUSIVE")
            self.helper_markers[name] = {"outcome": "PASS", "invocations": self.helper_invocations[name]}
        finally:
            self.target_input = None

    def test_result_canonical_bounds_and_receipt_reopen(self):
        normal = self.payload()
        reordered = {"queuedSubmission": dict(reversed(list(normal["queuedSubmission"].items())))}
        maximum = self.payload(self.data["unicode_character"] * 4096, client="C"*128, queued="Q"*128)
        reports = []
        for value in (normal, reordered, self.payload(spans=True), self.payload("\ufeffBOM-prefix"), maximum):
            before, identity = support.canonical(value), valid_identity(value)
            with mock.patch.object(product, "_canonical", wraps=product._canonical) as serializer:
                result = self.invoke(decoded_result=value, reason="ACK_RESULT_PREPARED")
            self.assertEqual(1, serializer.call_count)
            copied = serializer.call_args.args[0]
            self.assertIs(type(copied), dict)
            self.assertIsNot(copied, value)
            for parent, original in ((copied["queuedSubmission"], value["queuedSubmission"]),
                                      (copied["queuedSubmission"]["input"], value["queuedSubmission"]["input"]),
                                      (copied["queuedSubmission"]["input"][0], value["queuedSubmission"]["input"][0])):
                self.assertIs(type(parent), type(original))
                self.assertIsNot(parent, original)
            if "text_elements" in value["queuedSubmission"]["input"][0]:
                self.assertIsNot(copied["queuedSubmission"]["input"][0]["text_elements"], value["queuedSubmission"]["input"][0]["text_elements"])
            self.assertTrue(before == result["ack_result_bytes"], "EXACT_EXPECTED_ACK_BYTES_REQUIRED")
            self.assertEqual(support.sha(before), result["expected_ack_sha256"])
            self.assertTrue(before == support.canonical(value), "INPUT_BYTES_CHANGED")
            self.assertEqual(identity, valid_identity(value))
            self.assertTrue(value == json.loads(result["ack_result_bytes"]), "ACK_VALUES_CHANGED")
            self.assertTrue(1 <= len(value["queuedSubmission"]["input"][0]["text"].encode()) <= 16384)
            reports.append(result)
        self.assertTrue(reports[0]["ack_result_bytes"] == reports[1]["ack_result_bytes"], "KEY_ORDER_MUST_NOT_CHANGE_ACK_BYTES")
        self.assertIn(b'"text_elements":[]', reports[2]["ack_result_bytes"])
        self.compose_receipt(reports[0], outcome="ACK_CANDIDATE")
        self.compose_receipt(reports[-1], text=self.data["unicode_character"]*4096, client="C"*128, outcome="ACK_CANDIDATE")

    def test_invalid_result_shape_utf8_and_caps(self):
        self.case_utf8_byte_cap_rejects_overlong_result()
        values = [None, b"\xef\xbb\xbf{}", {}, [], DictSubclass(self.payload()), self.payload()]
        values[-1]["queuedSubmission"] = DictSubclass(values[-1]["queuedSubmission"])
        updates = [{"id": "x"*129}, {"clientUserMessageId": ""}, {"id": "bad id"},
                   {"id": StrSubclass("valid")}, {"input": ListSubclass([])}, {"extra": 1},
                   {"input": [{"type": "text", "text": StrSubclass("x")}]},
                   {"input": [{"type": "text", "text": "x", "text_elements": [1]}]},
                   {"input": [{"type": "text", "text": "x", "text_elements": ListSubclass()}]},
                   {"input": [{"type": "image", "text": "x"}]},
                   {"input": [{"type": "text", "text": v} for v in ("a", "b")]},
                   {"input": []}, {"input": ()}]
        for update in updates:
            value = self.payload()
            value["queuedSubmission"].update(update)
            values.append(value)
        counts = {"hooks": 0}
        for invalid in ("", "x"*16385, "\ud800", None, True, 2, 1.5, float("nan"), float("inf"), HookValue(counts)):
            value = self.payload()
            value["queuedSubmission"]["input"][0]["text"] = invalid
            values.append(value)
        cycle = self.payload()
        cycle["queuedSubmission"]["input"][0]["text"] = cycle
        values.append(cycle)
        for invalid in (True, 1, 1.5, float("nan"), float("inf"), HookValue(counts)):
            values.append(invalid)
        key_counts = {"hash": 0, "equality": 0}
        key = CollidingKey(key_counts)
        hostile = {key: self.payload()["queuedSubmission"]}
        key.armed = True
        key_counts.update(hash=0, equality=0)
        values.append(hostile)
        values.append({StrSubclass("queuedSubmission"): self.payload()["queuedSubmission"]})
        for index, value in enumerate(values):
            with self.subTest(index=index):
                with mock.patch.object(product, "_canonical", wraps=product._canonical) as serializer:
                    self.invoke(decoded_result=value, reason="RESULT_INVALID")
                self.assertEqual(0, serializer.call_count)
        self.assertEqual({"hooks": 0}, counts)
        self.assertEqual({"hash": 0, "equality": 0}, key_counts)
        for value, expected_encodes in (("x"*16385, 0), (StrSubclass("x"), 0), ("\ud800", 1)):
            candidate = self.payload(value)
            with mock.patch.object(product, "_encode_text", wraps=product._encode_text) as encoder, \
                    mock.patch.object(product, "_canonical", wraps=product._canonical) as serializer:
                self.invoke(decoded_result=candidate, reason="RESULT_INVALID")
            self.assertEqual(expected_encodes, encoder.call_count)
            self.assertEqual(0, serializer.call_count)
        self.assertEqual({"case_utf8_byte_cap_rejects_overlong_result": 1}, self.helper_invocations)

    def test_safe_error_absence_and_unknown_receipt(self):
        absence = self.invoke(reason="ABSENCE_PREPARED")
        original_classifier = quota._error_code
        representatives = {}
        counts = {"hooks": 0}
        examples = []
        for code in sorted(quota._ERRORS):
            error = Exception(PRIVATE)
            error.code = code
            examples.append((error, code, True))
        for value in ("UNKNOWN_PRIVATE_ERROR_CODE", "X"*100000, StrSubclass("RPC_REQUEST_TIMEOUT"), HookValue(counts)):
            error = Exception(PRIVATE)
            error.code = value
            examples.append((error, "UNCLASSIFIED_LOCAL_OR_TRANSPORT_ERROR", False))
        hostile = HostileError(PRIVATE)
        BaseException.__dict__["__dict__"].__get__(hostile)["code"] = "RPC_REQUEST_TIMEOUT"
        examples.append((hostile, "RPC_REQUEST_TIMEOUT", True))
        property_only = HostileError(PRIVATE)
        examples.append((property_only, "UNCLASSIFIED_LOCAL_OR_TRANSPORT_ERROR", False))
        key_counts = {"hash": 0, "equality": 0}
        key = CollidingKey(key_counts)
        collision = Exception(PRIVATE)
        BaseException.__dict__["__dict__"].__get__(collision)[key] = "RPC_REQUEST_TIMEOUT"
        key.armed = True
        key_counts.update(hash=0, equality=0)
        examples.append((collision, "UNCLASSIFIED_LOCAL_OR_TRANSPORT_ERROR", False))
        for count in (32, 33):
            error = Exception(PRIVATE)
            attributes = BaseException.__dict__["__dict__"].__get__(error)
            attributes["code"] = "RPC_REQUEST_TIMEOUT"
            attributes.update({"ignored_"+str(i): HookValue(counts) for i in range(count-1)})
            examples.append((error, "RPC_REQUEST_TIMEOUT" if count == 32 else "UNCLASSIFIED_LOCAL_OR_TRANSPORT_ERROR", count == 32))
        for index, (error, expected, has_code) in enumerate(examples):
            proxies = []

            def classify(proxy):
                self.assertIs(type(proxy), Exception)
                self.assertIsNot(proxy, error)
                self.assertEqual((), proxy.args)
                attributes = BaseException.__dict__["__dict__"].__get__(proxy)
                self.assertIs(type(attributes), dict)
                self.assertEqual({"code": expected} if has_code else {}, attributes)
                self.assertTrue(all(type(k) is str and type(v) is str for k, v in attributes.items()))
                proxies.append(proxy)
                return original_classifier(proxy)

            with self.subTest(index=index):
                with mock.patch.object(quota, "_error_code", side_effect=classify) as classifier:
                    prepared = self.invoke(call_error=error, reason="CALL_ERROR_PREPARED")
                self.assertEqual(1, classifier.call_count)
                self.assertEqual(1, len(proxies))
                self.assertEqual(expected, prepared["call_error_code"])
                representatives.setdefault(expected, prepared)
        self.assertEqual({"hooks": 0}, counts)
        self.assertEqual({"hash": 0, "equality": 0}, key_counts)
        for index, invalid in enumerate((None, "PRIVATE_EXCEPTION", object(), NoClassObject(), Exception,
                                         SystemExit(PRIVATE), KeyboardInterrupt(PRIVATE), GeneratorExit(PRIVATE))):
            with self.subTest(invalid_index=index):
                with mock.patch.object(quota, "_error_code", wraps=original_classifier) as classifier:
                    self.invoke(call_error=invalid, reason="ERROR_INVALID")
                self.assertEqual(0, classifier.call_count)
        for prepared in (absence, representatives["RPC_REQUEST_TIMEOUT"], representatives["UNCLASSIFIED_LOCAL_OR_TRANSPORT_ERROR"]):
            self.compose_receipt(prepared, outcome="UNKNOWN")

    def test_dual_none_and_no_io(self):
        self.case_dual_arguments_are_rejected()
        for result, error in ((None, None), (self.payload(), None), (None, Exception(PRIVATE)),
                              (HookValue({"hooks": 0}), HostileError(PRIVATE))):
            with mock.patch.object(product, "_result", wraps=product._result) as result_parser, \
                    mock.patch.object(product, "_error_code", wraps=product._error_code) as classifier, \
                    mock.patch.object(product, "_canonical", wraps=product._canonical) as serializer:
                self.invoke(decoded_result=result, call_error=error, reason="INPUTS_MUTUALLY_EXCLUSIVE")
            self.assertEqual(0, result_parser.call_count)
            self.assertEqual(0, classifier.call_count)
            self.assertEqual(0, serializer.call_count)
        self.invoke(decoded_result=None, reason="RESULT_INVALID")
        self.invoke(call_error=None, reason="ERROR_INVALID")
        self.invoke(decoded_result=product._MISSING, call_error=product._MISSING, reason="ABSENCE_PREPARED")
        with mock.patch.object(product, "_canonical", side_effect=Exception(PRIVATE)) as serializer:
            self.invoke(decoded_result=self.payload(), reason="PREPARATION_UNCONFIRMED")
        self.assertEqual(1, serializer.call_count)
        self.assertEqual({"case_dual_arguments_are_rejected": 1}, self.helper_invocations)
