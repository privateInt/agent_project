"""U01a-D29-v1: six pure synthetic admission tests, never queue real work."""
import builtins
import contextlib
import copy
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import types
import unittest
from unittest import mock

from src.ops.tools import codex_unattended_admission as product

FIXTURE = Path(__file__).resolve().parent / "fixtures/codex_unattended_admission/synthetic.json"
GATE_PATH = FIXTURE.parent / "flex_gate.py.txt"
GATE_SHA = "30419990ee0cd77dfe37923ac6fe051b6b47b94469a39cdf00452291b5daf16b"
SCHEDULE = None
SYNTHETIC = None
SETUP = {"gate_loads": 0}
OBSERVATIONS = []
DELETE = object()
HEADS = ("b" * 40, "c" * 40, "d" * 40)
DESIRED = {"approvalsReviewer": "auto_review", "approvalPolicy": {"granular": {
    "sandbox_approval": False, "request_permissions": False, "rules": False,
    "mcp_elicitations": False, "skill_approval": False}}}


class ForbiddenIO(BaseException):
    """Do not let product's ordinary Exception handler hide an IO attempt."""


def forbidden_dependency(*args, **kwargs):
    raise ForbiddenIO("NONPURE_GATE_ENTRYPOINT_FORBIDDEN")


def setUpModule():
    global SCHEDULE, SYNTHETIC
    if SCHEDULE is not None:
        return
    raw = GATE_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != GATE_SHA:
        raise AssertionError("Pinned gate source changed")
    gate = types.ModuleType("u01a_pinned_flex_gate")
    exec(compile(raw, str(GATE_PATH), "exec"), gate.__dict__)
    # Only these two actual functions are ever injected into selection.
    SCHEDULE = (gate.validate_config, gate.windows)
    for name in ("default_config", "decide", "read_config", "safe_json_file", "main"):
        setattr(gate, name, forbidden_dependency)
    SYNTHETIC = json.loads(FIXTURE.read_text())
    if SYNTHETIC["provenance"] != "SYNTHETIC_ONLY_NO_IO_NO_DISPATCH":
        raise AssertionError("Only explicit synthetic fixture configuration is allowed")
    SETUP.update(gate_loads=1, gate_sha256=GATE_SHA, gate_path=str(GATE_PATH),
                 injected_functions=[f.__name__ for f in SCHEDULE],
                 fixture_sha256=hashlib.sha256(FIXTURE.read_bytes()).hexdigest())


def milliseconds(value):
    return int(dt.datetime.fromisoformat(value).timestamp() * 1000)


def retime(fixture, now):
    snapshot = fixture[1]
    snapshot["now_ms"] = now
    for name in ("project", "pauses", "quota", "control", "settings"):
        snapshot[name]["observed_at_ms"] = now
    return fixture


def completion_bundle(config, task, admission):
    """Construct contract evidence from synthetic IDs, not product internals."""
    p = config["project"]
    binding = {"project_id": p["project_id"], "repository": p["repository"],
               "batch_id": config["batch_id"], "task_id": task["task_id"],
               "dev_root": p["dev_root"], "git_common_dir": p["git_common_dir"],
               "base_sha": admission["base_sha"], "head_sha": admission["head_sha"],
               "plan_sha256": task["plan_sha256"]}
    digest = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=True).encode("utf-8")).hexdigest()
    files = {task["allowed_files"][0]: hashlib.sha256(("synthetic:" + task["task_id"]).encode()).hexdigest()}
    return {"binding": binding, "terminal": {"binding_sha256": digest, "status": "completed"},
            "tests": {"binding_sha256": digest,
                      "results": {name: "PASS" for name in task["required_test_ids"]},
                      "failures": 0, "errors": 0, "skipped": 0},
            "review": {"binding_sha256": digest, "status": "PASS", "independent": True,
                       "implementer_id": "fixture-implementer", "reviewer_id": "fixture-reviewer"},
            "draft_pr": {"binding_sha256": digest,
                         "url": "https://github.com/" + p["repository"] + "/pull/123",
                         "draft": True, "base_sha": admission["base_sha"],
                         "head_sha": admission["head_sha"], "files_sha256": files},
            "readback": {"binding_sha256": digest, "status": "PASS",
                         "head_sha": admission["head_sha"], "files_sha256": dict(files)}}


def fixture(completed=0):
    c, s, state = (copy.deepcopy(SYNTHETIC[k]) for k in ("config", "snapshot", "state"))
    for index in range(completed):
        task = c["tasks"][index]
        admission = {"task_id": task["task_id"], "project_id": "agent_project",
                     "base_sha": c["project"]["base_sha"] if index == 0 else HEADS[index - 1],
                     "head_sha": HEADS[index], "plan_sha256": task["plan_sha256"],
                     "admitted_at_ms": s["now_ms"] - 60000 + index, "status": "COMPLETED"}
        state["admissions"].append(admission)
        state["evidence"][task["task_id"]] = completion_bundle(c, task, admission)
    if completed:
        s["project"]["value"]["head_sha"] = HEADS[completed - 1]
    return c, s, state


def changed(path, value, original=None):
    result = copy.deepcopy(original if original is not None else fixture())
    root = {"config": result[0], "snapshot": result[1], "state": result[2]}
    node = root
    for key in path[:-1]:
        node = node[key]
    if value is DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return result


def occupied(statuses):
    c, s, state = fixture()
    for index, status in enumerate(statuses):
        task = c["tasks"][index]
        state["admissions"].append({"task_id": task["task_id"], "project_id": "agent_project",
                                    "base_sha": c["project"]["base_sha"], "head_sha": None,
                                    "plan_sha256": task["plan_sha256"],
                                    "admitted_at_ms": s["now_ms"] - 1000, "status": status})
    return c, s, state


class NoClockDateTime(dt.datetime):
    @classmethod
    def now(cls, *args, **kwargs):
        raise ForbiddenIO("CLOCK_READ_FORBIDDEN")

    @classmethod
    def utcnow(cls, *args, **kwargs):
        raise ForbiddenIO("CLOCK_READ_FORBIDDEN")

    @classmethod
    def today(cls, *args, **kwargs):
        raise ForbiddenIO("CLOCK_READ_FORBIDDEN")


class AdmissionTests(unittest.TestCase):
    def select(self, inputs):
        before = copy.deepcopy(inputs)
        attempted = []

        def no_io(*args, **kwargs):
            attempted.append("FORBIDDEN_IO")
            raise ForbiddenIO("SELECTION_IO_FORBIDDEN")

        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            for module, names in (
                (builtins, ("open",)), (io, ("open",)),
                (os, ("open", "stat", "lstat", "listdir", "scandir")),
                (Path, ("read_bytes", "read_text", "write_bytes", "write_text", "exists", "is_file", "is_dir")),
                (socket, ("socket", "create_connection", "getaddrinfo")),
                (subprocess, ("Popen", "run", "check_output")),
                (time, ("time", "monotonic", "perf_counter", "process_time", "clock_gettime")),
            ):
                for name in names:
                    stack.enter_context(mock.patch.object(module, name, no_io))
            stack.enter_context(mock.patch.object(dt, "datetime", NoClockDateTime))
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(output))
            stack.enter_context(mock.patch.object(builtins, "__import__", no_io))
            answer = product.select_next_task(*inputs, schedule=SCHEDULE)
        self.last_decision = answer
        self.assertEqual(before, inputs)
        self.assertEqual([], attempted)
        self.assertEqual("", output.getvalue())
        self.assertEqual({"schema", "scope", "status", "reason", "candidate", "desired_settings",
                          "runtime_io_performed", "mutation_authorized"}, set(answer))
        self.assertEqual(1, answer["schema"])
        self.assertEqual("PURE_ADMISSION_ONLY", answer["scope"])
        self.assertIs(False, answer["runtime_io_performed"])
        self.assertIs(False, answer["mutation_authorized"])
        OBSERVATIONS.append({"test": self._testMethodName, "case": str(self._subtest),
                             "reason": answer["reason"], "status": answer["status"],
                             "candidate": answer["candidate"], "io_attempts": len(attempted)})
        return answer

    def expect(self, inputs, reason, task=None, base=None):
        answer = self.select(inputs)
        self.assertEqual(reason, answer["reason"])
        if task is None:
            self.assertEqual("NO_CANDIDATE" if reason == "NO_PENDING_TASK" else "BLOCKED", answer["status"])
            self.assertIsNone(answer["candidate"])
            self.assertIsNone(answer["desired_settings"])
        else:
            c = inputs[0]
            declared = next(t for t in c["tasks"] if t["task_id"] == task)
            expected = {k: c["project"][k] for k in
                        ("project_id", "repository", "dev_root", "git_common_dir")}
            expected.update(task_id=task, base_sha=base or c["project"]["base_sha"],
                            plan_sha256=declared["plan_sha256"])
            self.assertEqual("CANDIDATE", answer["status"])
            self.assertEqual(expected, answer["candidate"])
            self.assertEqual(DESIRED, answer["desired_settings"])
        return answer

    def test_input_binding_and_purity(self):
        self.case_portable_t1_t2_candidates()
        inputs = fixture()
        first = self.expect(inputs, "CANDIDATE_READY", "T1")
        self.assertEqual(first, self.select(inputs))
        self.assertEqual(1, SETUP["gate_loads"])
        self.assertEqual(["validate_config", "windows"], SETUP["injected_functions"])
        for path, value, reason in (
            (("config", "project", "project_id"), "vms_project", "PROJECT_NOT_ALLOWED"),
            (("snapshot", "project", "value", "project_id"), "other_project", "PROJECT_NOT_ALLOWED"),
            (("snapshot", "project", "value", "repository"), "other-owner/agent_project", "PROJECT_BINDING_MISMATCH"),
            (("snapshot", "project", "value", "dev_root"), "/fixture/other-root", "PROJECT_BINDING_MISMATCH"),
            (("snapshot", "project", "value", "git_common_dir"), "/fixture/other-git", "PROJECT_BINDING_MISMATCH"),
            (("snapshot", "project", "value", "base_sha"), "e" * 40, "PROJECT_BINDING_MISMATCH"),
            (("state", "batch_id"), "other-batch", "PROJECT_BINDING_MISMATCH"),
            (("config", "schema"), True, "INPUT_INVALID"),
            (("state", "schema"), True, "INPUT_INVALID"),
            (("snapshot", "now_ms"), True, "INPUT_INVALID"),
            (("snapshot", "pauses", "value", "root"), 0, "INPUT_INVALID"),
            (("snapshot", "quota", "value", "exhausted"), 0, "INPUT_INVALID"),
            (("config", "freshness", "max_age_ms"), True, "INPUT_INVALID"),
            (("config", "freshness", "max_age_ms"), 0, "INPUT_INVALID"),
            (("config", "freshness", "max_age_ms"), 300001, "INPUT_INVALID"),
            (("config", "tasks", 0, "timeout_sec"), True, "INPUT_INVALID"),
            (("config", "tasks", 0, "timeout_sec"), 29, "INPUT_INVALID"),
            (("config", "tasks", 0, "timeout_sec"), 7201, "INPUT_INVALID"),
            (("config", "tasks", 0, "plan_sha256"), "A" * 64, "INPUT_INVALID"),
            (("config", "project", "base_sha"), "a" * 39, "INPUT_INVALID"),
            (("config", "tasks", 1, "task_id"), "T1", "INPUT_INVALID"),
            (("config", "tasks", 1, "predecessor_id"), "wrong", "INPUT_INVALID"),
            (("config", "tasks", 0, "required_test_ids"), ["dup", "dup"], "INPUT_INVALID"),
            (("config", "tasks", 0, "allowed_files"), ["same.py", "same.py"], "INPUT_INVALID"),
            (("config", "tasks", 0, "required_test_ids"), [], "INPUT_INVALID"),
            (("config", "tasks", 0, "task_id"), "bad id", "INPUT_INVALID"),
            (("config", "tasks", 0, "task_id"), "x" * 129, "INPUT_INVALID"),
            (("config", "schedule", "version"), True, "INPUT_INVALID"),
            (("config", "schedule", "default", "enabled"), 1, "INPUT_INVALID"),
            (("config", "schedule", "default", "max_starts"), True, "INPUT_INVALID"),
            (("config", "freshness"), DELETE, "INPUT_INVALID"),
            (("config", "extra"), "PRIVATE_FIXTURE_MARKER", "INPUT_INVALID"),
            (("snapshot", "control", "value", "available"), "yes", "INPUT_INVALID"),
        ):
            with self.subTest(path=path, value=repr(value)):
                answer = self.expect(changed(path, value), reason)
                self.assertNotIn("PRIVATE_FIXTURE_MARKER", json.dumps(answer))
        for path in ("relative/root", "/fixture/./root", "/fixture/../root", "/fixture//root", "/fixture/root\x00"):
            with self.subTest(absolute_path=repr(path)):
                self.expect(changed(("config", "project", "dev_root"), path), "INPUT_INVALID")
        for path in ("/absolute.py", "src/../bad.py", "src/./bad.py", "src//bad.py", "bad\x00.py"):
            with self.subTest(relative_path=repr(path)):
                self.expect(changed(("config", "tasks", 0, "allowed_files"), [path]), "INPUT_INVALID")
        for timeout in (30, 7200):
            with self.subTest(timeout_boundary=timeout):
                self.expect(changed(("config", "tasks", 0, "timeout_sec"), timeout), "CANDIDATE_READY", "T1")
        for field, value in (("plan_sha256", "e" * 64), ("project_id", "other_project")):
            with self.subTest(admission_binding=field):
                self.expect(changed(("state", "admissions", 0, field), value, fixture(1)), "PROJECT_BINDING_MISMATCH")

    def case_portable_t1_t2_candidates(self):
        """The same pinned pure gate selects T1 and fully evidenced T2 anywhere."""
        self.expect(fixture(), "CANDIDATE_READY", "T1")
        self.expect(fixture(1), "CANDIDATE_READY", "T2", HEADS[0])
        self.assertEqual(1, SETUP["gate_loads"])

    def test_pause_and_freshness(self):
        for name in ("root", "user", "state"):
            for value, reason in ((True, name.upper() + "_PAUSED"), (None, "PAUSE_UNVERIFIED")):
                with self.subTest(pause=name, value=value):
                    self.expect(changed(("snapshot", "pauses", "value", name), value), reason)
        inputs = changed(("snapshot", "pauses", "value", "root"), True)
        inputs[1]["pauses"]["value"]["user"] = True
        inputs[1]["pauses"]["observed_at_ms"] -= 30001
        self.expect(inputs, "ROOT_PAUSED")
        inputs[1]["pauses"]["value"]["root"] = None
        self.expect(inputs, "PAUSE_UNVERIFIED")
        for name in ("project", "pauses", "quota", "control", "settings"):
            for age, reason in ((0, "CANDIDATE_READY"), (30000, "CANDIDATE_READY"),
                                (30001, "SNAPSHOT_STALE"), (-1, "SNAPSHOT_TIME_INVALID")):
                with self.subTest(observation=name, age=age):
                    inputs = fixture()
                    inputs[1][name]["observed_at_ms"] = inputs[1]["now_ms"] - age
                    self.expect(inputs, reason, "T1" if reason == "CANDIDATE_READY" else None)
            for value in (DELETE, True, "yesterday"):
                with self.subTest(observation=name, bad_timestamp=repr(value)):
                    self.expect(changed(("snapshot", name, "observed_at_ms"), value), "INPUT_INVALID")
        inputs = fixture()
        inputs[2]["clock_floor_ms"] = inputs[1]["now_ms"] + 1
        self.expect(inputs, "SNAPSHOT_TIME_INVALID")
        inputs = fixture(1)
        inputs[2]["admissions"][0]["admitted_at_ms"] = inputs[1]["now_ms"] + 1
        self.expect(inputs, "SNAPSHOT_TIME_INVALID")

    def test_schedule_quota_and_settings(self):
        for stamp, reason in (("2024-01-01T23:00:00+09:00", "CANDIDATE_READY"),
                              ("2024-01-02T00:00:00+09:00", "CANDIDATE_READY"),
                              ("2024-01-02T07:00:00+09:00", "OUTSIDE_WINDOW")):
            with self.subTest(schedule_boundary=stamp):
                self.expect(retime(fixture(), milliseconds(stamp)), reason,
                            "T1" if reason == "CANDIDATE_READY" else None)
        end = milliseconds("2024-01-02T07:00:00+09:00")
        for delta, reason in ((-1, "CANDIDATE_READY"), (0, "CANDIDATE_READY"), (1, "WINDOW_TOO_SHORT")):
            with self.subTest(margin_delta_ms=delta):
                self.expect(retime(fixture(), end - 720000 + delta), reason,
                            "T1" if reason == "CANDIDATE_READY" else None)
        inputs = retime(fixture(), end - 720000 + 1)
        inputs[1]["quota"]["value"]["ordinaryUsageAllowed"] = False
        inputs[1]["control"]["value"]["desired_settings_supported"] = False
        self.expect(inputs, "WINDOW_TOO_SHORT")
        inputs = fixture()
        inputs[0]["schedule"]["dates"]["2024-01-01"] = {
            "enabled": False, "start": "23:00", "end": "07:00", "max_starts": 3}
        self.expect(inputs, "OUTSIDE_WINDOW")
        # The latest-starting overlapping date override controls cap counting.
        inputs = fixture(1)
        inputs[0]["schedule"]["dates"]["2024-01-02"] = {
            "enabled": True, "start": "00:00", "end": "02:00", "max_starts": 1}
        inputs[2]["admissions"][0]["admitted_at_ms"] = milliseconds("2024-01-02T00:00:00+09:00")
        self.expect(inputs, "WINDOW_CAP_REACHED")
        inputs[2]["admissions"][0]["admitted_at_ms"] -= 1
        self.expect(inputs, "CANDIDATE_READY", "T2", HEADS[0])
        inputs = fixture(1)
        inputs[0]["schedule"]["default"]["max_starts"] = 1
        self.expect(inputs, "WINDOW_CAP_REACHED")
        for path, value, reason in (
            (("quota", "ordinaryUsageAllowed"), False, "USAGE_UNVERIFIED"),
            (("quota", "ordinaryUsageAllowed"), None, "USAGE_UNVERIFIED"),
            (("quota", "exhausted"), True, "USAGE_UNVERIFIED"),
            (("quota", "exhausted"), None, "USAGE_UNVERIFIED"),
            (("control", "available"), False, "CONTROL_UNAVAILABLE"),
            (("control", "available"), None, "CONTROL_UNAVAILABLE"),
            (("control", "desired_settings_supported"), False, "SETTINGS_UNVERIFIED"),
            (("control", "desired_settings_supported"), None, "SETTINGS_UNVERIFIED"),
            (("settings", "cwd"), "/fixture/other", "SETTINGS_UNVERIFIED"),
            (("settings", "runtimeWorkspaceRoots"), ["/fixture/other"], "SETTINGS_UNVERIFIED"),
            (("settings", "approvalsReviewer"), "unknown", "SETTINGS_UNVERIFIED"),
            (("settings", "approvalPolicy"), "never", "SETTINGS_UNVERIFIED"),
            (("settings", "sandbox", "networkAccess"), True, "SETTINGS_UNVERIFIED"),
            (("settings", "sandbox", "networkAccess"), 0, "SETTINGS_UNVERIFIED"),
            (("settings", "sandbox", "type"), "dangerFullAccess", "SETTINGS_UNVERIFIED"),
            (("settings", "sandbox", "writableRoots"), ["/fixture/other"], "SETTINGS_UNVERIFIED"),
        ):
            with self.subTest(gate=path, value=value):
                self.expect(changed(("snapshot", path[0], "value") + path[1:], value), reason)
        for reviewer in ("user", "auto_review"):
            with self.subTest(accepted_reviewer=reviewer):
                inputs = changed(("snapshot", "settings", "value", "approvalPolicy"), copy.deepcopy(DESIRED["approvalPolicy"]))
                inputs[1]["settings"]["value"]["approvalsReviewer"] = reviewer
                self.expect(inputs, "CANDIDATE_READY", "T1")
        for flag in DESIRED["approvalPolicy"]["granular"]:
            for value in (True, 0):
                with self.subTest(granular_flag=flag, value=value):
                    policy = copy.deepcopy(DESIRED["approvalPolicy"])
                    policy["granular"][flag] = value
                    self.expect(changed(("snapshot", "settings", "value", "approvalPolicy"), policy), "SETTINGS_UNVERIFIED")

    def test_state_and_t1_t2_evidence(self):
        for status in ("RESERVED", "QUEUED", "ACTIVE"):
            with self.subTest(occupied=status):
                self.expect(occupied([status]), "ACTIVE_TASK")
        self.expect(occupied(["ACTIVE", "QUEUED"]), "STATE_CONFLICT")
        for status in ("UNKNOWN", "SUBMISSION_UNKNOWN", "START_UNKNOWN", "CHECKPOINT_UNCONFIRMED"):
            with self.subTest(unknown=status):
                self.expect(occupied([status]), "UNKNOWN_STATE")
        for status in ("FAILED", "INTERRUPTED"):
            with self.subTest(predecessor=status):
                self.expect(occupied([status]), "PREDECESSOR_NOT_COMPLETED")
        self.expect(fixture(1), "CANDIDATE_READY", "T2", HEADS[0])
        empty = fixture()
        empty[0]["tasks"] = []
        self.expect(empty, "NO_PENDING_TASK")
        done = fixture(1)
        done[0]["tasks"] = done[0]["tasks"][:1]
        self.expect(done, "NO_PENDING_TASK")
        for section in ("binding", "terminal", "tests", "review", "draft_pr", "readback"):
            with self.subTest(missing_bundle_section=section):
                self.expect(changed(("state", "evidence", "T1", section), DELETE, fixture(1)), "EVIDENCE_UNVERIFIED")
        for path, value in (
            (("binding", "project_id"), "other_project"),
            (("binding", "batch_id"), "other-batch"),
            (("binding", "plan_sha256"), "e" * 64),
            (("binding", "base_sha"), "e" * 40),
            (("binding", "head_sha"), "e" * 40),
            (("terminal", "status"), "failed"),
            (("tests", "results"), {"unit.T1": "PASS"}),
            (("tests", "results"), {"unit.T1": "PASS", "contract.T1": "SKIP"}),
            (("tests", "results"), {"unit.T1": "FAIL", "contract.T1": "PASS"}),
            (("tests", "binding_sha256"), "e" * 64),
            (("tests", "failures"), 1), (("tests", "errors"), 1),
            (("tests", "skipped"), 1), (("tests", "skipped"), False),
            (("review", "independent"), False), (("review", "status"), "FAIL"),
            (("review", "status"), "NOT_RUN"),
            (("review", "reviewer_id"), "fixture-implementer"),
            (("review", "reviewer_id"), ""),
            (("draft_pr", "draft"), False),
            (("draft_pr", "url"), "https://github.com/other-owner/agent_project/pull/123"),
            (("draft_pr", "url"), "https://github.com/fixture-owner/agent_project/pull/123?extra=1"),
            (("draft_pr", "head_sha"), "e" * 40),
            (("draft_pr", "base_sha"), "e" * 40),
            (("draft_pr", "files_sha256"), {}),
            (("draft_pr", "files_sha256"), {"outside.py": "e" * 64}),
            (("readback", "status"), "PENDING"), (("readback", "status"), "NOT_RUN"),
            (("readback", "status"), "FAIL"),
            (("readback", "head_sha"), "e" * 40),
            (("readback", "files_sha256"), {"src/ops/tools/fixture_T1.py": "e" * 64}),
        ):
            with self.subTest(evidence_field=path, value=value):
                self.expect(changed(("state", "evidence", "T1") + path, value, fixture(1)), "EVIDENCE_UNVERIFIED")
        self.expect(changed(("snapshot", "project", "value", "head_sha"), HEADS[1], fixture(1)), "HEAD_BINDING_MISMATCH")
        consumed = fixture(2)
        consumed[2]["admissions"][1].update(status="FAILED", head_sha=None)
        consumed[2]["evidence"].pop("T2")
        consumed[1]["project"]["value"]["head_sha"] = HEADS[0]
        for admission in consumed[2]["admissions"]:
            admission["admitted_at_ms"] -= 86400000
        self.expect(consumed, "LIFETIME_CAP_REACHED")

    def test_lifetime_cap_blocks_third_candidate(self):
        # Single M1 target: all other gates valid; no dispatch exists here.
        inputs = fixture(2)
        self.expect(inputs, "LIFETIME_CAP_REACHED")

    def test_t2_pending_review_blocks(self):
        # Single M2 target: review status is the sole unmet completion condition.
        inputs = changed(("state", "evidence", "T1", "review", "status"), "PENDING", fixture(1))
        self.expect(inputs, "EVIDENCE_UNVERIFIED")
