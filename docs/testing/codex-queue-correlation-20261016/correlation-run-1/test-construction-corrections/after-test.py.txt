"""U01b2-D29-v2: synthetic snapshots, real borrowed temporary SQLite reads."""
import contextlib
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from src.ops.tools import codex_unattended_intent_journal as journal
from src.ops.tools import codex_unattended_queue_observer as product
from src.ops.tools.tests import test_codex_unattended_intent_journal as support


FIXTURES = Path(__file__).resolve().parent / "fixtures/codex_unattended_queue_observer"
canonical, sha = support.canonical, support.sha
OBSERVATIONS = []
FALSE_FLAGS = {"input_origin_verified", "native_acceptance_verified", "remote_io_performed",
               "remote_mutation_authorized", "replay_permitted", "native_ready"}
REPORT_KEYS = FALSE_FLAGS | {"scope", "status", "reason", "journal_read_confirmed", "journal_reason",
                            "journal_state", "journal_revision", "pages_examined", "items_examined",
                            "pagination_complete"}
BLOCKED = {"ARGUMENTS_INVALID", "INPUT_HASH_MISMATCH", "INPUT_INVALID", "JOURNAL_BLOCKED",
           "INTENT_BINDING_MISMATCH", "INTENT_NOT_UNKNOWN"}
UNKNOWN = {"JOURNAL_UNCONFIRMED", "EVIDENCE_INCOMPLETE", "OBSERVATION_INVALID",
           "CORRELATION_AMBIGUOUS", "CONTENT_MISMATCH", "ABSENT_UNRESOLVED"}
SCHEMA_PINS = {
    "ThreadQueueAddParams.json": "60f25b7d3e3357c215bef9d2fe7047200f545e030dca9e00527ac0620cd0c88f",
    "ThreadQueueListParams.json": "b4c889089eee8a0f785ff85e0128a4d3f347353870fa848221096a472223773b",
    "ThreadQueueListResponse.json": "be4e0bfa6fc776101f96582543605d2eff6186309a08903203512b99c5005926",
}


def raw(value):
    return value if type(value) is bytes else canonical(value)


def packed(request, response):
    request = raw(request)
    response = None if response is None else raw(response)
    return request, sha(request), response, None if response is None else sha(response)


def padded(value, size):
    value = raw(value)
    if len(value) > size:
        raise AssertionError("Padding fixture exceeds its target size")
    return value + b" " * (size - len(value))


class QueueObserverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        manifest = json.loads((FIXTURES / "manifest.json").read_text())
        for name, pin in manifest["files"].items():
            if sha((FIXTURES / name).read_bytes()) != pin:
                raise AssertionError("Relative synthetic/schema fixture pin mismatch")
        for name, pin in SCHEMA_PINS.items():
            if manifest["files"][name] != pin:
                raise AssertionError("Native schema bytes changed")
        add = json.loads((FIXTURES / "ThreadQueueAddParams.json").read_bytes())
        request = json.loads((FIXTURES / "ThreadQueueListParams.json").read_bytes())
        response = json.loads((FIXTURES / "ThreadQueueListResponse.json").read_bytes())
        if (set(add["required"]) != {"threadId", "clientUserMessageId", "input"}
                or request["required"] != ["threadId"] or response["required"] != ["data"]
                or add["definitions"]["UserInput"] != response["definitions"]["UserInput"]):
            raise AssertionError("Pinned generated schema contract mismatch")
        cls.fixture = json.loads((FIXTURES / "synthetic.json").read_text())
        if cls.fixture["provenance"] != "SYNTHETIC_ONLY_OFFLINE_CORRELATION_NO_REMOTE_IO":
            raise AssertionError("Non-synthetic fixture forbidden")
        value = cls.fixture["intent"]
        if (sha(canonical(value)) != cls.fixture["canonical_intent_sha256"]
                or value["prompt_sha256"] != sha(cls.fixture["prompt"].encode("utf-8"))
                or value["project_sha256"] != sha(canonical({k: value[k] for k in support.PROJECT_KEYS}))
                or value["task_sha256"] != sha(canonical(cls.fixture["task_descriptor"]))
                or value["settings_sha256"] != sha(canonical(cls.fixture["settings_fixture"]))):
            raise AssertionError("Independent synthetic intent bindings invalid")

    @contextlib.contextmanager
    def database(self, *, claimed=True, prompt=None, client=None):
        value = copy.deepcopy(self.fixture["intent"])
        prompt = self.fixture["prompt"] if prompt is None else prompt
        value["prompt_sha256"] = sha(prompt.encode("utf-8"))
        if client is not None:
            value["clientUserMessageId"] = client
        intent = canonical(value)
        with tempfile.TemporaryDirectory(prefix="u01b2-offline-sqlite-") as directory:
            path = Path(directory) / "intent.sqlite"
            connection = support.connect(path)
            try:
                self.assertEqual("JOURNAL_READY", journal.initialize_intent_journal(connection)["report"]["reason"])
                self.assertEqual("INTENT_STORED", journal.reserve_intent(connection, sha(intent), intent)["report"]["reason"])
                key = support.intent_key(value)
                if claimed:
                    self.assertEqual("CLAIM_RECORDED", journal.claim_intent(connection, key, sha(intent), 0)["report"]["reason"])
                original = support.rows(connection)
                db = {"connection": connection, "path": path, "value": value, "raw": intent,
                      "key": key, "prompt": prompt, "state": "SUBMISSION_UNKNOWN" if claimed else "PREPARED",
                      "revision": 1 if claimed else 0}
                yield db
                connection.close()
                with contextlib.closing(support.connect(path)) as reopened:
                    self.assertEqual(original, support.rows(reopened))
                    stored = support.rows(reopened)[0]
                    self.assertEqual(intent, stored[4])
                    self.assertEqual(sha(intent), stored[5])
                    self.assertEqual((db["state"], db["revision"]), stored[6:])
            finally:
                connection.close()

    def submission(self, db, **changes):
        value = {"threadId": self.fixture["thread_id"],
                 "clientUserMessageId": db["value"]["clientUserMessageId"],
                 "input": [{"type": "text", "text": db["prompt"]}]}
        value.update(changes)
        return value

    def item(self, db, *, queue=None, client=None, text=None, elements=False):
        part = {"type": "text", "text": db["prompt"] if text is None else text}
        if elements:
            part["text_elements"] = []
        return {"id": self.fixture["queue_id"] if queue is None else queue,
                "clientUserMessageId": db["value"]["clientUserMessageId"] if client is None else client,
                "input": [part]}

    def page(self, db, items=None, *, cursor=None, next_cursor=None, thread=None, limit=20):
        request = {"threadId": self.fixture["thread_id"] if thread is None else thread,
                   "cursor": cursor, "limit": limit}
        return packed(request, {"data": [self.item(db)] if items is None else items, "nextCursor": next_cursor})

    def invoke(self, db, *, pages=None, submission=None, reason="CANDIDATE_OBSERVED", reads=1,
               binding=True, confirmed=True, candidate=None, key=None, intent_pin=None, submission_pin=None):
        submission = raw(self.submission(db)) if submission is None else submission
        submission = raw(submission) if type(submission) is dict else submission
        pages = (self.page(db),) if pages is None else pages
        intent_pin = sha(db["raw"]) if intent_pin is None else intent_pin
        submission_pin = sha(submission) if submission_pin is None and type(submission) is bytes else submission_pin
        expected_binding = None
        if binding:
            manifest = {"schema": 1, "intent_sha256": intent_pin,
                        "submission_params_sha256": submission_pin,
                        "pages": [{"request_sha256": p[1], "response_sha256": p[3]} for p in pages]}
            expected_binding = {"intent_sha256": intent_pin, "submission_params_sha256": submission_pin,
                                "observation_sha256": sha(canonical(manifest))}
        connection, path = db["connection"], db["path"]
        before, original_profile = support.database_snapshot(connection, path), support.profile(connection)
        read_calls, forbidden_writes, direct_sql, sql = [], [], [], []
        depth = [0]
        read_code = journal.read_intent.__code__
        write_codes = {getattr(journal, n).__code__ for n in
                       ("initialize_intent_journal", "reserve_intent", "claim_intent")}

        def profile_call(frame, event, argument):
            if frame.f_code is read_code:
                if event == "call":
                    read_calls.append("read_intent")
                    depth[0] += 1
                elif event == "return":
                    depth[0] -= 1
            if event == "call" and frame.f_code in write_codes:
                forbidden_writes.append("JOURNAL_WRITE_API")
                raise support.ForbiddenIO("OBSERVER_WRITE_API_FORBIDDEN")

        def trace(statement):
            (sql if depth[0] else direct_sql).append(statement)

        previous = sys.getprofile()
        connection.set_trace_callback(trace)
        try:
            sys.setprofile(profile_call)
            with support.no_external_io() as (attempts, output):
                result = product.observe_queue_correlation(
                    connection, db["key"] if key is None else key, intent_pin,
                    submission_params_bytes=submission, expected_submission_sha256=submission_pin, pages=pages)
            self.last_result = result  # Retain actual wrong candidate before semantic assertion.
        finally:
            sys.setprofile(previous)
            connection.set_trace_callback(None)
        self.assertEqual([], attempts)
        self.assertEqual("", output.getvalue())
        self.assertEqual([], forbidden_writes)
        self.assertEqual([], direct_sql)
        self.assertEqual(reads, len(read_calls))
        self.assertEqual(0, depth[0])
        for statement in sql:
            upper = statement.strip().upper()
            self.assertTrue(upper.startswith(("SELECT ", "PRAGMA ")) or upper in ("BEGIN", "COMMIT", "ROLLBACK"))
            if upper.startswith("PRAGMA "):
                self.assertNotIn("=", upper)
        self.assertEqual(original_profile, support.profile(connection))
        self.assertEqual(before, support.database_snapshot(connection, path))
        self.assertFalse(connection.in_transaction)
        connection.execute("SELECT 1").close()
        self.assertIs(type(result), dict)
        self.assertEqual({"report", "binding", "candidate"}, set(result))
        report = result["report"]
        self.assertIs(type(report), dict)
        self.assertEqual(REPORT_KEYS, set(report))
        self.assertEqual("OFFLINE_QUEUE_CORRELATION_ONLY", report["scope"])
        self.assertIn(report["reason"], BLOCKED | UNKNOWN | {"CANDIDATE_OBSERVED"})
        status = "BLOCKED" if report["reason"] in BLOCKED else (
            "UNKNOWN" if report["reason"] in UNKNOWN else "OBSERVED")
        self.assertEqual(status, report["status"])
        for name in FALSE_FLAGS | {"journal_read_confirmed", "pagination_complete"}:
            self.assertIs(type(report[name]), bool)
        for name in FALSE_FLAGS:
            self.assertIs(False, report[name])
        self.assertIs(confirmed, report["journal_read_confirmed"])
        self.assertIn(report["journal_reason"], support.CONFIRMED | support.BLOCKED | {"CHECKPOINT_UNCONFIRMED", None})
        if confirmed:
            self.assertEqual("INTENT_OBSERVED", report["journal_reason"])
            self.assertEqual(db["state"], report["journal_state"])
            self.assertEqual(db["revision"], report["journal_revision"])
            self.assertIs(type(report["journal_revision"]), int)
        else:
            self.assertIsNone(report["journal_state"])
            self.assertIsNone(report["journal_revision"])
        for name, maximum in (("pages_examined", 4), ("items_examined", 80)):
            self.assertIs(type(report[name]), int)
            self.assertGreaterEqual(report[name], 0)
            self.assertLessEqual(report[name], maximum)
        self.assertEqual(expected_binding, result["binding"])
        if status == "OBSERVED":
            self.assertTrue(report["pagination_complete"])
            self.assertEqual({"thread_id", "queued_submission_id", "client_user_message_id"}, set(result["candidate"]))
            for val in result["candidate"].values():
                self.assertIs(type(val), str)
                self.assertRegex(val, r"\A[A-Za-z0-9_.:-]{1,128}\Z")
            self.assertEqual(json.loads(submission)["threadId"], result["candidate"]["thread_id"])
            self.assertEqual(db["value"]["clientUserMessageId"], result["candidate"]["client_user_message_id"])
        else:
            self.assertIsNone(result["candidate"])
        public = json.dumps(result, sort_keys=True, ensure_ascii=True)
        for marker in ("SYNTHETIC_CORRELATION_PROMPT_NEVER_LOG", "SYNTHETIC_OPAQUE_CURSOR_NEVER_LOG",
                       "WRONG_PRIVATE_PROMPT_NEVER_LOG", support.PRIVATE_ERROR, self.fixture["other_client"],
                       self.fixture["other_queue"], db["value"]["dev_root"], '"input"', '"text"', '"data"', '"cursor"'):
            self.assertNotIn(marker, public)
        OBSERVATIONS.append({"test": self._testMethodName, "case": str(self._subtest),
                             "result": result, "read_calls": len(read_calls), "direct_sql": len(direct_sql),
                             "sql_count": len(sql), "database_unchanged": True, "external_io_attempts": len(attempts)})
        self.assertEqual(reason, report["reason"])  # Semantic mutation failure must occur here.
        if reason == "CANDIDATE_OBSERVED":
            self.assertEqual(candidate or {"thread_id": self.fixture["thread_id"],
                                          "queued_submission_id": self.fixture["queue_id"],
                                          "client_user_message_id": db["value"]["clientUserMessageId"]}, result["candidate"])
        return result

    def test_valid_observation_and_read_only_boundary(self):
        with self.database() as db:
            result = self.invoke(db)
            self.assertEqual((1, 1), (result["report"]["pages_examined"], result["report"]["items_examined"]))
            pages = (self.page(db, [self.item(db, client=self.fixture["other_client"], queue=self.fixture["other_queue"])],
                               next_cursor=self.fixture["cursor"]),
                     self.page(db, cursor=self.fixture["cursor"]))
            result = self.invoke(db, pages=pages)
            self.assertEqual((2, 2), (result["report"]["pages_examined"], result["report"]["items_examined"]))
            expanded = json.dumps(self.submission(db), ensure_ascii=True, indent=2).encode()
            expanded_page = packed(json.dumps(json.loads(self.page(db)[0]), indent=3).encode(),
                                   json.dumps({"nextCursor": None, "data": [self.item(db, elements=True)]}, indent=2).encode())
            first = self.invoke(db)
            second = self.invoke(db, submission=expanded, pages=(expanded_page,))
            self.assertNotEqual(first["binding"]["submission_params_sha256"], second["binding"]["submission_params_sha256"])
            self.assertNotEqual(first["binding"]["observation_sha256"], second["binding"]["observation_sha256"])
            self.assertEqual(first["candidate"], second["candidate"])
            self.invoke(db, intent_pin="0" * 64, reason="JOURNAL_BLOCKED", confirmed=False)
            self.assertEqual("INTENT_PIN_MISMATCH", self.last_result["report"]["journal_reason"])
            actual_one, attempts = journal._one, []

            def fail_read(connection, query):
                if query == "PRAGMA busy_timeout":
                    attempts.append(query)
                    raise sqlite3.OperationalError(support.PRIVATE_ERROR)
                return actual_one(connection, query)

            with mock.patch.object(journal, "_one", side_effect=fail_read):
                self.invoke(db, reason="JOURNAL_UNCONFIRMED", confirmed=False)
            self.assertEqual(["PRAGMA busy_timeout"], attempts)
        with self.database(claimed=False) as db:
            self.invoke(db, reason="INTENT_NOT_UNKNOWN")

    def test_unknown_and_bounded_evidence(self):
        with self.database() as db:
            cases = [
                ("empty-complete", (self.page(db, []),), "ABSENT_UNRESOLVED"),
                ("other-client", (self.page(db, [self.item(db, client=self.fixture["other_client"], queue=self.fixture["other_queue"])]),), "ABSENT_UNRESOLVED"),
                ("no-evidence", (), "EVIDENCE_INCOMPLETE"),
                ("lost-response", (packed({"threadId": self.fixture["thread_id"], "cursor": None, "limit": 20}, None),), "EVIDENCE_INCOMPLETE"),
                ("partial", (self.page(db, next_cursor=self.fixture["cursor"]),), "EVIDENCE_INCOMPLETE"),
                ("cursor-omitted", (packed({"threadId": self.fixture["thread_id"], "cursor": None, "limit": 20}, {"data": [self.item(db)]}),), "EVIDENCE_INCOMPLETE"),
                ("later-loss", (self.page(db, next_cursor=self.fixture["cursor"]), packed({"threadId": self.fixture["thread_id"], "cursor": self.fixture["cursor"], "limit": 20}, None)), "EVIDENCE_INCOMPLETE"),
                ("duplicate-id", (self.page(db, [self.item(db), self.item(db)]),), "CORRELATION_AMBIGUOUS"),
                ("two-client-matches", (self.page(db, [self.item(db), self.item(db, queue=self.fixture["second_queue_id"])]),), "CORRELATION_AMBIGUOUS"),
                ("wrong-thread", (self.page(db, thread="fixture-other-thread"),), "OBSERVATION_INVALID"),
                ("bool-limit", (self.page(db, limit=True),), "OBSERVATION_INVALID"),
                ("wrong-limit", (self.page(db, limit=19),), "OBSERVATION_INVALID"),
                ("after-eof", (self.page(db), self.page(db, [])), "OBSERVATION_INVALID"),
                ("bad-chain", (self.page(db, next_cursor="cursor-A"), self.page(db, cursor="cursor-B")), "OBSERVATION_INVALID"),
                ("cursor-cycle", (self.page(db, [], next_cursor="cursor-A"), self.page(db, cursor="cursor-A", next_cursor="cursor-A")), "OBSERVATION_INVALID"),
                ("first-not-null", (self.page(db, cursor="cursor-A"),), "OBSERVATION_INVALID"),
                ("empty-cursor", (self.page(db, next_cursor=""),), "OBSERVATION_INVALID"),
            ]
            for label, pages, reason in cases:
                with self.subTest(snapshot=label):
                    result = self.invoke(db, pages=pages, reason=reason)
                    if reason == "ABSENT_UNRESOLVED":
                        self.assertTrue(result["report"]["pagination_complete"])
                    if reason == "EVIDENCE_INCOMPLETE":
                        self.assertFalse(result["report"]["pagination_complete"])
            for label, part in (("image", {"type": "image", "url": "synthetic://not-supported"}),
                                ("spans", {"type": "text", "text": db["prompt"], "text_elements": [{"byteRange": {"start": 0, "end": 1}}]}),
                                ("extra", {"type": "text", "text": db["prompt"], "extra": True})):
                with self.subTest(unsupported=label):
                    item = self.item(db, client=self.fixture["other_client"], queue=self.fixture["other_queue"])
                    item["input"] = [part]
                    self.invoke(db, pages=(self.page(db, [self.item(db), item]),), reason="OBSERVATION_INVALID")
                    self.invoke(db, submission=self.submission(db, input=[part]), reason="INPUT_INVALID", reads=0, confirmed=False)
            for label, changed in (("trim", db["prompt"] + " "), ("newline", db["prompt"].replace("\r\n", "\n")),
                                   ("normalization", db["prompt"].replace("e\u0301", "é"))):
                with self.subTest(exact_text=label):
                    self.invoke(db, pages=(self.page(db, [self.item(db, text=changed)]),), reason="CONTENT_MISMATCH")
            self.invoke(db, submission=self.submission(db, clientUserMessageId="fixture-different-client"), reason="INTENT_BINDING_MISMATCH")
            self.invoke(db, submission=self.submission(db, input=[{"type": "text", "text": "WRONG_PRIVATE_PROMPT_NEVER_LOG"}]), reason="INTENT_BINDING_MISMATCH")
            for field in ("submission", "request", "response"):
                with self.subTest(hash_tampering=field):
                    page = list(self.page(db))
                    kwargs = {"reason": "INPUT_HASH_MISMATCH", "reads": 0, "confirmed": False, "binding": False}
                    if field == "submission":
                        kwargs["submission_pin"] = "0" * 64
                    else:
                        page[1 if field == "request" else 3] = "0" * 64
                        kwargs["pages"] = (tuple(page),)
                    self.invoke(db, **kwargs)
            bad_json = [b"{", b"\xef\xbb\xbf{}", b"{\xff}", b'{"x":1,"x":2}', b'{"x":NaN}',
                        b'{"x":Infinity}', b'{"x":1e400}', b'{"x":1.0}', b'{"x":123456789012345678901234567890}',
                        b'[' * 128 + b'0' + b']' * 128]
            for index, bad in enumerate(bad_json):
                for field in ("submission", "request", "response"):
                    with self.subTest(early_json=index, field=field):
                        # Frozen v2 parses submission before read, page JSON after it.
                        kwargs = {"reason": "INPUT_INVALID" if field == "submission" else "OBSERVATION_INVALID",
                                  "reads": 0 if field == "submission" else 1,
                                  "confirmed": field != "submission"}
                        if field == "submission":
                            kwargs["submission"] = bad
                        elif field == "request":
                            kwargs["pages"] = (packed(bad, {"data": [], "nextCursor": None}),)
                        else:
                            kwargs["pages"] = (packed(json.loads(self.page(db)[0]), bad),)
                        self.invoke(db, **kwargs)
            equality = []

            class Hostile:
                def __eq__(self, other):
                    equality.append("INVOKED")
                    raise AssertionError("Type validation must precede hostile equality")

            for kwargs in ({"submission": bytearray(canonical(self.submission(db)))},
                           {"pages": list((self.page(db),))}, {"pages": (list(self.page(db)),)},
                           {"submission_pin": Hostile()}, {"intent_pin": Hostile()},
                           {"key": (Hostile(), "batch", "task")}, {"pages": (self.page(db) + (None,),)},
                           {"pages": ((self.page(db)[0], "0", None, None),)},
                           {"pages": ((self.page(db)[0], self.page(db)[1], None, "0" * 64),)}):
                with self.subTest(outer_type=list(kwargs)):
                    self.invoke(db, reason="ARGUMENTS_INVALID", reads=0, confirmed=False, binding=False, **kwargs)
                    self.assertEqual([], equality)
            normal_sub, normal_page = canonical(self.submission(db)), self.page(db)
            for field, maximum in (("submission", 131072), ("request", 4096), ("response", 262144)):
                for extra in (0, 1):
                    with self.subTest(byte_boundary=field, extra=extra):
                        kwargs = {}
                        if field == "submission":
                            kwargs["submission"] = padded(normal_sub, maximum + extra)
                        elif field == "request":
                            kwargs["pages"] = (packed(padded(normal_page[0], maximum + extra), normal_page[2]),)
                        else:
                            kwargs["pages"] = (packed(normal_page[0], padded(normal_page[2], maximum + extra)),)
                        if extra:
                            kwargs.update(reason="ARGUMENTS_INVALID", reads=0, confirmed=False, binding=False)
                        self.invoke(db, **kwargs)
            chain = tuple(self.page(db, [self.item(db)] if i == 0 else [], cursor=None if i == 0 else "c%d" % i,
                                    next_cursor=None if i == 3 else "c%d" % (i + 1)) for i in range(4))
            self.invoke(db, pages=chain)
            self.invoke(db, pages=chain + (self.page(db, []),), reason="ARGUMENTS_INVALID", reads=0, confirmed=False, binding=False)
            incomplete = list(chain)
            incomplete[-1] = self.page(db, [], cursor="c3", next_cursor="c4")
            self.invoke(db, pages=tuple(incomplete), reason="EVIDENCE_INCOMPLETE")
            total_pages = tuple(packed(padded(p[0], 4096), padded(p[2], 262144 if i < 3 else 114688)) for i, p in enumerate(chain))
            big_sub = padded(normal_sub, 131072)
            self.assertEqual(1048576, len(big_sub) + sum(len(p[0]) + len(p[2]) for p in total_pages))
            self.invoke(db, submission=big_sub, pages=total_pages)
            over = list(total_pages)
            over[-1] = packed(over[-1][0], over[-1][2] + b" ")
            self.invoke(db, submission=big_sub, pages=tuple(over), reason="ARGUMENTS_INVALID", reads=0, confirmed=False, binding=False)
            for count in (20, 21):
                items = [self.item(db)] + [self.item(db, queue="foreign-Q-%d" % i, client="foreign-client-%d" % i) for i in range(1, count)]
                with self.subTest(page_items=count):
                    self.invoke(db, pages=(self.page(db, items),), reason="CANDIDATE_OBSERVED" if count == 20 else "OBSERVATION_INVALID")
            all_pages = []
            for i in range(4):
                items = [self.item(db, queue="eighty-Q-%d" % (i * 20 + j), client="eighty-client-%d" % (i * 20 + j)) for j in range(20)]
                if i == 0:
                    items[0] = self.item(db)
                all_pages.append(self.page(db, items, cursor=None if i == 0 else "e%d" % i, next_cursor=None if i == 3 else "e%d" % (i + 1)))
            result = self.invoke(db, pages=tuple(all_pages))
            self.assertEqual((4, 80), (result["report"]["pages_examined"], result["report"]["items_examined"]))
            for size in (1024, 1025):
                cursor = "가" * 341 + "a" * (size - 1023)
                with self.subTest(cursor_utf8_bytes=size):
                    self.invoke(db, pages=(self.page(db, [], next_cursor=cursor), self.page(db, cursor=cursor)),
                                reason="CANDIDATE_OBSERVED" if size == 1024 else "OBSERVATION_INVALID")
            for size in (128, 129):
                with self.subTest(queue_id_length=size):
                    queue = "Q" * size
                    self.invoke(db, pages=(self.page(db, [self.item(db, queue=queue)]),),
                                reason="CANDIDATE_OBSERVED" if size == 128 else "OBSERVATION_INVALID",
                                candidate={"thread_id": self.fixture["thread_id"], "queued_submission_id": queue,
                                           "client_user_message_id": db["value"]["clientUserMessageId"]})
                with self.subTest(thread_id_length=size):
                    thread = "T" * size
                    self.invoke(db, submission=self.submission(db, threadId=thread), pages=(self.page(db, thread=thread),),
                                reason="CANDIDATE_OBSERVED" if size == 128 else "INPUT_INVALID", reads=1 if size == 128 else 0,
                                confirmed=size == 128, candidate={"thread_id": thread, "queued_submission_id": self.fixture["queue_id"],
                                                                  "client_user_message_id": db["value"]["clientUserMessageId"]})
            self.invoke(db, submission=self.submission(db, input=[]), reason="INPUT_INVALID", reads=0, confirmed=False)
            for field in ("threadId", "clientUserMessageId"):
                with self.subTest(empty_identifier=field):
                    self.invoke(db, submission=self.submission(db, **{field: ""}), reason="INPUT_INVALID", reads=0, confirmed=False)
            self.invoke(db, pages=(self.page(db, [self.item(db, queue="")]),), reason="OBSERVATION_INVALID")
        long_prompt = "SYNTHETIC_CORRELATION_PROMPT_NEVER_LOG" + "p" * (16384 - len("SYNTHETIC_CORRELATION_PROMPT_NEVER_LOG"))
        with self.database(prompt=long_prompt, client="C" * 128) as db:
            self.invoke(db)
            self.invoke(db, submission=self.submission(db, input=[{"type": "text", "text": long_prompt + "x"}]), reason="INPUT_INVALID", reads=0, confirmed=False)
            self.invoke(db, pages=(self.page(db, [self.item(db, text=long_prompt + "x")]),), reason="OBSERVATION_INVALID")
            self.invoke(db, submission=self.submission(db, clientUserMessageId="C" * 129), reason="INPUT_INVALID", reads=0, confirmed=False)
            self.invoke(db, pages=(self.page(db, [self.item(db, client="C" * 129)]),), reason="OBSERVATION_INVALID")
            self.invoke(db, submission=self.submission(db, input=[{"type": "text", "text": ""}]), reason="INPUT_INVALID", reads=0, confirmed=False)
            self.invoke(db, pages=(self.page(db, [self.item(db, text="")]),), reason="OBSERVATION_INVALID")
        with self.database(prompt="p", client="C") as db:
            self.invoke(db, submission=self.submission(db, threadId="T"), pages=(self.page(db, [self.item(db, queue="Q")], thread="T"),),
                        candidate={"thread_id": "T", "queued_submission_id": "Q", "client_user_message_id": "C"})

    def test_matching_client_wrong_prompt_remains_unknown(self):
        # M1 only: all byte pins, journal/client/thread/schema/unique bindings valid.
        with self.database() as db:
            self.invoke(db, pages=(self.page(db, [self.item(db, text="WRONG_PRIVATE_PROMPT_NEVER_LOG")]),), reason="CONTENT_MISMATCH")

    def test_two_matching_queue_ids_remain_ambiguous(self):
        # M2 only: complete two-page chain, distinct IDs, identical intended text.
        with self.database() as db:
            pages = (self.page(db, next_cursor=self.fixture["cursor"]),
                     self.page(db, [self.item(db, queue=self.fixture["second_queue_id"])], cursor=self.fixture["cursor"]))
            self.invoke(db, pages=pages, reason="CORRELATION_AMBIGUOUS")
